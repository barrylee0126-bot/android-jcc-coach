// Public integration regression test for the generated replay-coach page.
// Requires Node.js, the project Python environment, and Chrome/Chromium.
// Configure executables with JCC_REPLAY_COACH_PYTHON and
// JCC_REPLAY_COACH_CHROME_BIN when auto-discovery is unsuitable.
import assert from "node:assert/strict";
import { access, chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { deflateSync } from "node:zlib";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const PROJECT = resolve(HERE, "../../..");
const PYTHON = process.env.JCC_REPLAY_COACH_PYTHON
  || join(PROJECT, ".venv", "bin", "python");
const CHROME_CANDIDATES = process.env.JCC_REPLAY_COACH_CHROME_BIN
  ? [process.env.JCC_REPLAY_COACH_CHROME_BIN]
  : [
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Chromium.app/Contents/MacOS/Chromium",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
    ];

async function executable(path) {
  try {
    await access(path, constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

function crc32(data) {
  let value = 0xffffffff;
  for (const byte of data) {
    value ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      value = (value >>> 1) ^ (value & 1 ? 0xedb88320 : 0);
    }
  }
  return (value ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const name = Buffer.from(type, "ascii");
  const size = Buffer.alloc(4);
  size.writeUInt32BE(data.length);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([name, data])));
  return Buffer.concat([size, name, data, crc]);
}

function onePixelPng(red, green, blue) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(1, 0);
  header.writeUInt32BE(1, 4);
  header[8] = 8;
  header[9] = 2;
  return Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(Buffer.from([0, red, green, blue]))),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

function sha256(data) {
  return createHash("sha256").update(data).digest("hex");
}

function injectedPage(page, { brokenCss }) {
  const override = brokenCss
    ? "<style>article[hidden]{display:grid!important}</style>"
    : "";
  const probe = `<script>
(() => {
  const arithmetic = "31 − 2 = 29";
  const visible = () => [...document.querySelectorAll("article")]
    .filter((node) => {
      const style = getComputedStyle(node);
      const rect = node.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden"
        && Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
    })
    .map((node) => ({
      sequence: node.dataset.sequence,
      text: node.innerText,
      display: getComputedStyle(node).display,
      rect: [node.getBoundingClientRect().width, node.getBoundingClientRect().height],
    }));
  const stages = [];
  const capture = (name) => stages.push({name, cards: visible()});
  capture("initial");
  document.querySelector("#next").click(); capture("unknown_one");
  document.querySelector("#next").click(); capture("unknown_two");
  document.querySelector("#previous").click();
  document.querySelector("#previous").click(); capture("return_first");
  const output = document.createElement("pre");
  output.id = "visibility-test-result";
  output.textContent = JSON.stringify({arithmetic, stages});
  document.body.append(output);
})();
</script>`;
  return page.replace("<main>", `${override}<main>`).replace("</html>", `${probe}</html>`);
}

function inspectDump(dump) {
  const match = dump.match(/<pre id="visibility-test-result">([\s\S]*?)<\/pre>/);
  assert.ok(match, "browser did not emit visibility-test-result");
  return JSON.parse(match[1]);
}

function assertFixed(result) {
  const expected = ["1", "2", "3", "1"];
  assert.deepEqual(result.stages.map((stage) => stage.cards.map((card) => card.sequence)),
                   expected.map((sequence) => [sequence]));
  assert.match(result.stages[0].cards[0].text, /31 − 2 = 29/);
  assert.doesNotMatch(result.stages[1].cards[0].text, /31 − 2 = 29/);
  assert.doesNotMatch(result.stages[2].cards[0].text, /31 − 2 = 29/);
  assert.match(result.stages[3].cards[0].text, /31 − 2 = 29/);
}

async function writeSyntheticManifest(root) {
  const colors = [[255, 0, 0], [0, 255, 0], [0, 0, 255]];
  const paths = [];
  for (const [index, color] of colors.entries()) {
    const path = join(root, `synthetic-${index + 1}.png`);
    await writeFile(path, onePixelPng(...color), {mode: 0o600});
    await chmod(path, 0o600);
    paths.push(path);
  }
  const digests = await Promise.all(paths.map(async (path) => sha256(await readFile(path))));
  const frames = paths.map((path, index) => ({
    id: `synthetic_${index + 1}`,
    requested_time_s: index + 1,
    decoded_pts_s: null,
    path,
    sha256: digests[index],
    candidate: index === 0
      ? {status: "machine_candidate", source: "video-economy-candidate-v1", value: 31,
         source_frame_sha256: digests[index], hypothetical_spend: 2}
      : {status: "unknown", reason: "new_frame_without_reliable_candidate",
         basis: "no_fresh_candidate_supplied"},
  }));
  const manifest = {schema_version: "replay-coach-v1", purpose: "private_replay_condition_demo",
                    source_kind: "recovered_recording_development", frames};
  const path = join(root, "manifest.json");
  await writeFile(path, JSON.stringify(manifest), {mode: 0o600});
  await chmod(path, 0o600);
  return path;
}

function renderWithProject(manifest, output) {
  const environment = {...process.env, PYTHONPATH: join(PROJECT, "src")};
  execFileSync(PYTHON, ["-B", "-c", [
    "from jcc_replay_coach.core import build_replay_coach",
    "import sys",
    "build_replay_coach(sys.argv[1], sys.argv[2])",
  ].join(";"), manifest, output], {cwd: PROJECT, env: environment, stdio: "pipe"});
}

function renderInBrowser(chrome, page, profile) {
  const output = execFileSync(chrome, [
    "--headless=new", "--disable-gpu", "--no-sandbox", `--user-data-dir=${profile}`,
    "--virtual-time-budget=1000", "--dump-dom", pathToFileURL(page).href,
  ], {encoding: "utf8", stdio: ["ignore", "pipe", "pipe"]});
  return inspectDump(output);
}

const chrome = (await Promise.all(CHROME_CANDIDATES.map(async (path) => (await executable(path) ? path : null))))
  .find(Boolean);
if (!chrome) {
  console.error(JSON.stringify({
    status: "skipped", reason: "chromium_or_chrome_not_found",
    configure: "set JCC_REPLAY_COACH_CHROME_BIN to an executable Chrome/Chromium path",
  }));
  process.exitCode = 77;
} else if (!(await executable(PYTHON))) {
  console.error(JSON.stringify({
    status: "skipped", reason: "project_python_not_found",
    configure: "set JCC_REPLAY_COACH_PYTHON to the project Python executable",
  }));
  process.exitCode = 77;
} else {
  const root = await mkdtemp(join(tmpdir(), "jcc-replay-coach-web-"));
  await chmod(root, 0o700);
  try {
    const manifest = await writeSyntheticManifest(root);
    const rendered = join(root, "rendered");
    renderWithProject(manifest, rendered);
    const original = await readFile(join(rendered, "report.html"), "utf8");
    const fixed = join(root, "fixed.html");
    const broken = join(root, "broken.html");
    await writeFile(fixed, injectedPage(original, {brokenCss: false}), {mode: 0o600});
    await writeFile(broken, injectedPage(original, {brokenCss: true}), {mode: 0o600});
    await chmod(fixed, 0o600);
    await chmod(broken, 0o600);

    const fixedResult = renderInBrowser(chrome, fixed, join(root, "chrome-fixed"));
    assertFixed(fixedResult);
    const brokenResult = renderInBrowser(chrome, broken, join(root, "chrome-broken"));
    assert.equal(brokenResult.stages[0].cards.length, 3,
                 "negative control must expose all hidden cards under the old CSS override");
    assert.match(brokenResult.stages[0].cards[0].text, /31 − 2 = 29/);
    console.log(JSON.stringify({
      status: "browser_visibility_verified",
      input: "synthetic_public",
      browser: basename(chrome),
      fixed_transition: "candidate_to_unknown_to_unknown_to_candidate",
      negative_control: "old_hidden_override_detected_three_visible_cards",
    }));
  } finally {
    await rm(root, {recursive: true, force: true});
  }
}
