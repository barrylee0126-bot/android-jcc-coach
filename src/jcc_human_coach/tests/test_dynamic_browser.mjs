// Loopback browser check: JSONL append -> dynamic page -> double-rAF report -> local lease withdrawal.
// It has no tablet, replay image, account, or private capture input.
import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile, appendFile } from "node:fs/promises";
import { spawn, execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const project = resolve(here, "../../..");
const python = join(project, ".venv", "bin", "python");
const chrome = [process.env.JCC_HUMAN_COACH_CHROME_BIN, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/usr/bin/chromium"].find(Boolean);
if (!chrome) { console.error(JSON.stringify({status:"skipped",reason:"chrome_not_found"})); process.exitCode=77; }
else {
  const root = await mkdtemp(join(tmpdir(), "jcc-human-coach-dynamic-")); await chmod(root, 0o700);
  const input = join(root, "input.jsonl"); await writeFile(input, "", {mode:0o600});
  const proc = spawn(python, ["-B", "-m", "jcc_human_coach", "--input", input, "--stream-id", "browser-stream", "--clock-domain-id", "browser-host", "--source-timeout-ms", "10000"], {cwd:project, env:{...process.env,PYTHONPATH:join(project,"src")}, stdio:["ignore","pipe","pipe"]});
  let output=""; proc.stdout.on("data", c => {output += c;});
  try {
    const until=Date.now()+3000; while(!output.includes("\n") && Date.now()<until) await new Promise(r=>setTimeout(r,20));
    const first=JSON.parse(output.trim().split("\n")[0]); assert.equal(first.delivery,"loopback_browser_only");
    const now=Math.floor(Number(process.hrtime.bigint()/1000000n));
    const event={schema_version:"human-coach-v1",source_kind:"replay",stream_id:"browser-stream",frame_id:"fixture-frame-browser",sequence:1,source_sequence:9,frame_received_ms:now-10,emitted_ms:now-5,expires_ms:now+9000,status:"candidate_visible",reason:"candidate",context_version:null,evidence_id:"fixture-evidence-browser",evidence_status:"machine_candidate",advice:null,observation:{observation_id:"observe-only",title_zh:"虚构机器观察",facts_zh:"计数候选与标签候选。",unknown_zh:"比例、场景、规则和策略未知。"}};
    await appendFile(input, JSON.stringify(event)+"\n");
    let view; for(let i=0;i<80;i++){view=await (await fetch(first.url+"api/view")).json();if(view.visible)break;await new Promise(r=>setTimeout(r,25));}
    assert.equal(view.visible,true);
    const dom=execFileSync(chrome,["--headless=new","--disable-gpu","--run-all-compositor-stages-before-draw","--no-sandbox",`--user-data-dir=${join(root,"chrome")}`,"--virtual-time-budget=1800","--dump-dom",first.url],{encoding:"utf8",stdio:["ignore","pipe","pipe"]});
    assert.match(dom,/提示已撤回：响应已过期|提示已撤回：候选字段不完整/);
    assert.doesNotMatch(dom,/机器候选：场上 4\/5/);
    const report=await (await fetch(first.url+"api/report")).json();
    assert.equal(report.delivery,"loopback_browser_only");
    assert.equal(report.tablet_delivery_verified,false);
    const paint = report.browser_reports.some(row=>row.stage==="paint_opportunity" && row.fresh);
    console.log(JSON.stringify({status:"dynamic_browser_verified",transitions:"jsonl_candidate_slow_response_withdrawn",paint_opportunity_received:paint,delivery:report.delivery,tablet_delivery_verified:false}));
  } finally { proc.kill("SIGTERM"); await rm(root,{recursive:true,force:true}); }
}
