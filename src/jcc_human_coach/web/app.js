"use strict";
(() => {
  const byId = id => document.getElementById(id), card = byId("card");
  const categories = {lineup:"羁绊/阵容",economy:"转型/经济",equipment:"装备",positioning:"站位",selection:"选择"};
  const idOK = v => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$/.test(v);
  const intOK = v => typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
  const clientId = "browser-" + crypto.randomUUID();
  let session=null, revision=-1, retired=-1, current=null, eventSeq=0, leaseTimer=null, polling=false, epoch=0;
  const put=(id,value)=>{byId(id).textContent=value;};
  const clearTimer=()=>{if(leaseTimer!==null){clearTimeout(leaseTimer);leaseTimer=null;}};
  const elapsed=(mono,wall)=>{const a=performance.now()-mono,b=Date.now()-wall;return a<0||b<0?Infinity:Math.max(a,b);};
  function clear(reason){clearTimer();if(current)retired=Math.max(retired,current.revision);current=null;card.dataset.state="withdrawn";put("state","提示已撤回："+reason);put("source","");put("title","");put("action","");put("reason","不沿用上一条动作、理由或数字。");put("expiry","");}
  function report(stage,shown,domToRaf=null){if(!shown)return;const body={session_id:shown.session,client_id:clientId,client_event_sequence:++eventSeq,revision:shown.revision,render_token:shown.token,opportunity_id:shown.opportunity,stage,visibility:document.hidden?"hidden":"visible",browser_monotonic_ms:performance.now(),dom_to_raf_ms:domToRaf};fetch("/api/browser-event",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),keepalive:true,cache:"no-store"}).catch(()=>{});}
  function validView(v){return v&&v.schema_version==="human-coach-view-v1"&&idOK(v.session_id)&&idOK(v.stream_id)&&idOK(v.render_token)&&intOK(v.revision)&&typeof v.visible==="boolean"&&intOK(v.remaining_ms)&&intOK(v.lease_ms);}
  function sourceText(kind,status){const source=kind==="replay"||kind==="replay_candidate"?"回放来源（调用方声明）":kind==="live_candidate"?"实时来源（调用方声明）":kind==="synthetic"?"虚构演示来源（调用方声明）":"来源未知";return source+" · 证据状态（调用方声明）："+(typeof status==="string"?status:"未知");}
  function receive(v,fetchMono,fetchWall){
    if(!validView(v)){clear("不可信视图");return;}
    if(session!==null&&v.session_id!==session){clear("会话已更换");revision=-1;retired=-1;}
    session=v.session_id;if(v.revision<revision)return;revision=v.revision;
    if(!v.visible){retired=Math.max(retired,v.revision);clear(v.reason||"未知");return;}
    const age=elapsed(fetchMono,fetchWall),remaining=v.remaining_ms-age,lease=v.lease_ms-age,a=v.advice,o=v.observation;
    const observation=o&&idOK(o.id)&&typeof o.title_zh==="string"&&typeof o.facts_zh==="string"&&typeof o.unknown_zh==="string";
    const advice=a&&idOK(a.id)&&categories[a.category]&&typeof a.title_zh==="string"&&typeof a.action_zh==="string"&&typeof a.reason_zh==="string";
    if(!Number.isFinite(age)||!idOK(v.opportunity_id)||(Boolean(observation)===Boolean(advice))||remaining<=0||lease<=0||v.revision<=retired){clear(remaining<=0?"响应已过期":"候选字段不完整");return;}
    if(v.evidence_status==="machine_candidate"&&!observation){clear("机器候选只能观察");return;}
    if(document.hidden)return;
    if(current&&current.token===v.render_token&&current.revision===v.revision)return;
    if(current)report("withdrawn",current);clearTimer();
    const shown=current={session,revision:v.revision,token:v.render_token,opportunity:v.opportunity_id};
    card.dataset.state="visible";put("state",observation?"仅观察，不提供建议":"当前建议候选");put("source",sourceText(v.source_kind,v.evidence_status));put("title",observation?o.title_zh:a.title_zh);put("action",observation?o.facts_zh:a.action_zh);put("reason",observation?"缺失："+o.unknown_zh:a.reason_zh);put("expiry","本地显示租期：约 "+Math.ceil(Math.min(remaining,lease)/1000)+" 秒");
    report("dom_updated",shown);const start=performance.now();requestAnimationFrame(()=>requestAnimationFrame(()=>{if(current!==shown||document.hidden)return;const box=card.getBoundingClientRect();if(box.width>0&&box.height>0)report("paint_opportunity",shown,Math.max(0,performance.now()-start));}));
    leaseTimer=setTimeout(()=>{if(current===shown){report("withdrawn",shown);clear("本地租期到期");}},Math.min(remaining,lease));
  }
  async function poll(){if(polling||document.hidden)return;polling=true;const mark=++epoch,mono=performance.now(),wall=Date.now(),abort=new AbortController(),timer=setTimeout(()=>abort.abort(),800);try{const r=await fetch("/api/view",{cache:"no-store",signal:abort.signal}),v=await r.json();if(mark===epoch&&r.ok)receive(v,mono,wall);else if(mark===epoch)clear("服务不可用");}catch(_){if(mark===epoch)clear("本机连接中断");}finally{clearTimeout(timer);polling=false;}}
  document.addEventListener("visibilitychange",()=>{if(document.hidden)clear("页面不可见");else poll();});window.addEventListener("pagehide",()=>clear("页面离开"));setInterval(poll,125);poll();
})();
