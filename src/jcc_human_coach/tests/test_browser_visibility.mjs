// Public browser regression for the generated player-card page. It uses the
// built-in example cards only; no replay image, tablet, account, or device is
// read. Chrome/Chromium is optional and a missing binary reports a skip.

import assert from "node:assert/strict";
import { access, chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const project = resolve(here, "../../..");
const python = join(project, ".venv", "bin", "python");
const choices = process.env.JCC_HUMAN_COACH_CHROME_BIN
  ? [process.env.JCC_HUMAN_COACH_CHROME_BIN]
  : ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"];

async function executable(file) {
  try { await access(file, constants.X_OK); return true; } catch { return false; }
}

function build(root) {
  const output = join(root, "demo");
  execFileSync(python, ["-B", "-m", "jcc_human_coach", "--demo-output", output], {
    cwd: project, env: {...process.env, PYTHONPATH: join(project, "src")}, stdio: "pipe",
  });
  return join(output, "player-coach-demo.html");
}

function injectProbe(page) {
  const probe = `<script>
  (() => {
    const records = [];
    const state = () => document.querySelector('#state').innerText;
    const capture = name => records.push({name, text: state()});
    capture('initial');
    document.querySelector('#next').click(); capture('replay_population');
    document.querySelector('#next').click(); capture('synthetic_synergy');
    document.querySelector('#next').click(); capture('synthetic_equipment');
    document.querySelector('#next').click(); capture('synthetic_transition');
    document.querySelector('#next').click(); capture('unknown_withdrawn');
    document.querySelector('#previous').click();
    setTimeout(() => {
      capture('expired_after_local_countdown');
      const result = document.createElement('pre');
      result.id = 'human-coach-browser-result';
      result.textContent = JSON.stringify(records);
      document.body.append(result);
    }, 2300);
  })();
  </script>`;
  return page.replace("</html>", `${probe}</html>`);
}

function recordsFrom(dom) {
  const match = dom.match(/<pre id="human-coach-browser-result">([\s\S]*?)<\/pre>/);
  assert.ok(match, "browser did not finish the expiry probe");
  return JSON.parse(match[1]);
}

const chrome = (await Promise.all(choices.map(async file => await executable(file) ? file : null))).find(Boolean);
if (!chrome || !(await executable(python))) {
  console.error(JSON.stringify({status: "skipped", reason: chrome ? "project_python_not_found" : "chromium_or_chrome_not_found"}));
  process.exitCode = 77;
} else {
  const root = await mkdtemp(join(tmpdir(), "jcc-human-coach-browser-"));
  await chmod(root, 0o700);
  try {
    const source = await readFile(build(root), "utf8");
    const checked = join(root, "checked.html");
    await writeFile(checked, injectProbe(source), {mode: 0o600});
    await chmod(checked, 0o600);
    const dom = execFileSync(chrome, [
      "--headless=new", "--disable-gpu", "--no-sandbox", `--user-data-dir=${join(root, "chrome")}`,
      "--virtual-time-budget=3500", "--dump-dom", pathToFileURL(checked).href,
    ], {encoding: "utf8", stdio: ["ignore", "pipe", "pipe"]});
    const byName = Object.fromEntries(recordsFrom(dom).map(record => [record.name, record.text]));
    assert.match(byName.initial, /自然庇护所/);
    assert.match(byName.initial, /站位/);
    assert.doesNotMatch(byName.initial, /positioning/);
    assert.match(byName.replay_population, /先补满人口/);
    assert.doesNotMatch(byName.replay_population, /围绕植物/);
    assert.match(byName.synthetic_synergy, /3 永恒之森/);
    assert.match(byName.synthetic_equipment, /巨人杀手/);
    assert.match(byName.synthetic_transition, /2 迅捷射手/);
    assert.match(byName.unknown_withdrawn, /提示已撤回/);
    assert.doesNotMatch(byName.unknown_withdrawn, /阵容转型/);
    assert.match(byName.expired_after_local_countdown, /candidate_expired/);
    assert.doesNotMatch(byName.expired_after_local_countdown, /2 迅捷射手/);
    console.log(JSON.stringify({status: "browser_visibility_verified", browser: basename(chrome),
      transitions: "replay_to_synthetic_to_unknown_withdrawn_and_local_expiry"}));
  } finally {
    await rm(root, {recursive: true, force: true});
  }
}
