"""A narrow presentation contract for a human-operated game coach.

This module deliberately does not read the tablet, identify a unit, decide a
strategy, or inject an input event.  It receives a short, already-produced
candidate card and makes one current card visible to the player.  A new frame,
unknown input, a conflicting context, or expiry withdraws the old card.

The strict boundary is intentional: a future state/strategy adapter can be
reviewed independently of the player-facing expiry behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
import html
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Callable
from uuid import uuid4


SCHEMA = "human-coach-v1"
SOURCE_KINDS = {"synthetic", "replay", "replay_candidate", "live_candidate"}
VISIBLE = "candidate_visible"
WITHDRAWALS = {"unknown", "stale", "conflict", "scene_changed", "disconnected", "expired"}
KINDS = {VISIBLE, *WITHDRAWALS}
CATEGORIES = {"lineup", "economy", "equipment", "positioning", "selection"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}\Z")
PROJECT = Path(__file__).resolve().parents[2]


class CoachContractError(ValueError):
    """A fixed reason code; never include raw screen, account, or device data."""


def _fail(code: str) -> None:
    raise CoachContractError(code)


def _identifier(value: object) -> bool:
    return type(value) is str and ID.fullmatch(value) is not None


def _text(value: object, maximum: int) -> bool:
    return (type(value) is str and 0 < len(value) <= maximum and value == value.strip()
            and not any(ord(char) < 32 for char in value))


def _time(value: object) -> bool:
    return type(value) is int and not isinstance(value, bool) and 0 <= value <= 2**63 - 1


def _finite_number(value: object, maximum: float) -> bool:
    if type(value) not in {int, float} or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= maximum
    except (OverflowError, ValueError):
        return False


@dataclass(frozen=True)
class CoachAdvice:
    """Short text intended for a player, not an operation or an action command."""

    advice_id: str
    category: str
    title_zh: str
    action_zh: str
    reason_zh: str


@dataclass(frozen=True)
class CoachObservation:
    """Bounded scene facts for a player, deliberately not an action suggestion."""

    observation_id: str
    title_zh: str
    facts_zh: str
    unknown_zh: str


@dataclass(frozen=True)
class CoachEvent:
    """One source-frame-bound candidate or an explicit withdrawal barrier."""

    schema_version: str
    source_kind: str
    stream_id: str
    frame_id: str
    sequence: int
    frame_received_ms: int
    emitted_ms: int
    expires_ms: int
    status: str
    reason: str
    advice: CoachAdvice | None
    context_version: str | None = None
    evidence_id: str | None = None
    source_sequence: int | None = None
    evidence_status: str = "unverified"
    observation: CoachObservation | None = None


def _valid_advice(value: object) -> bool:
    return (type(value) is CoachAdvice and _identifier(value.advice_id)
            and type(value.category) is str and value.category in CATEGORIES and _text(value.title_zh, 32)
            and _text(value.action_zh, 42) and _text(value.reason_zh, 96))


def _valid_observation(value: object) -> bool:
    return (type(value) is CoachObservation and _identifier(value.observation_id)
            and _text(value.title_zh, 48) and _text(value.facts_zh, 96) and _text(value.unknown_zh, 96))


def _validate_event(event: object) -> str | None:
    if type(event) is not CoachEvent:
        return "event_type"
    if (event.schema_version != SCHEMA or type(event.source_kind) is not str
            or event.source_kind not in SOURCE_KINDS):
        return "schema_or_source"
    if not all(_identifier(value) for value in (event.stream_id, event.frame_id)):
        return "identifier"
    if not _time(event.sequence) or event.sequence < 1:
        return "sequence"
    if not all(_time(value) for value in (event.frame_received_ms, event.emitted_ms, event.expires_ms)):
        return "time_type"
    if not event.frame_received_ms <= event.emitted_ms < event.expires_ms:
        return "time_order"
    if event.expires_ms - event.emitted_ms > 10_000:
        return "lease_too_long"
    if (type(event.status) is not str or type(event.reason) is not str
            or event.status not in KINDS or event.reason not in WITHDRAWALS | {"candidate"}):
        return "status_or_reason"
    if event.context_version is not None and not _identifier(event.context_version):
        return "context_version"
    if event.evidence_id is not None and not _identifier(event.evidence_id):
        return "evidence_id"
    if event.source_sequence is not None and (not _time(event.source_sequence) or event.source_sequence < 1):
        return "source_sequence"
    if type(event.evidence_status) is not str or event.evidence_status not in {"unverified", "machine_candidate", "independently_reviewed"}:
        return "evidence_status"
    if event.status == VISIBLE:
        advice_present, observation_present = event.advice is not None, event.observation is not None
        if advice_present == observation_present:
            return "candidate_mutual_exclusion"
        if event.reason != "candidate":
            return "candidate_shape"
        if advice_present:
            if not _valid_advice(event.advice):
                return "advice_shape"
            if event.evidence_status == "machine_candidate":
                return "machine_candidate_observation_only"
            # A synthetic fixture may demonstrate wording, but a replay/live
            # machine candidate must never become an advice merely by label.
            if event.source_kind != "synthetic" and (event.evidence_status != "independently_reviewed"
                                                       or event.context_version is None or event.evidence_id is None):
                return "advice_evidence_insufficient"
        elif not _valid_observation(event.observation):
            return "observation_shape"
    elif event.advice is not None or event.observation is not None or event.reason == "candidate":
        return "withdrawal_shape"
    return None


class HumanCoachStore:
    """One latest-frame suggestion stream; invalid input fails closed.

    The supplied `clock` is only a local rendering clock.  Source kind, frame
    freshness, game phase, strategy evidence, and version suitability remain
    caller declarations until a future adapter proves them.
    """

    def __init__(self, *, stream_id: str | None = None,
                 clock: Callable[[], int] | None = None,
                 clock_domain_id: str | None = None,
                 session_id: str | None = None) -> None:
        self.stream_id = stream_id or "coach-stream-" + uuid4().hex
        if not _identifier(self.stream_id):
            raise ValueError("invalid_stream")
        self.clock_domain_id = clock_domain_id or "host-mono-" + uuid4().hex
        self.session_id = session_id or "coach-session-" + uuid4().hex
        if not _identifier(self.clock_domain_id) or not _identifier(self.session_id):
            raise ValueError("invalid_session_or_clock_domain")
        self._clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self._last_clock: int | None = None
        self._last_sequence = 0
        self._last_source_sequence: int | None = None
        self._last_frame = -1
        self._frame_ids: set[str] = set()
        self._last_advice_id: str | None = None
        self._revision = 0
        self._current: CoachEvent | None = None
        self._status = "unknown"
        self._reason = "waiting_for_candidate"
        self._closed = False
        self._render_token = "render-0"
        self._last_source_ingress: int | None = None
        self._opportunities: list[dict] = []
        self._current_opportunity: dict | None = None
        self._browser_reports: list[dict] = []
        self._browser_report_ids: set[tuple[str, int]] = set()
        self._counters = {"accepted_events": 0, "rejected_inputs": 0,
                          "browser_reports_accepted": 0, "browser_reports_historical": 0}

    def _now(self) -> int | None:
        try:
            now = self._clock()
        except Exception:
            now = None
        if not _time(now) or (self._last_clock is not None and now < self._last_clock):
            self._closed = True
            self._withdraw("unknown", "local_clock_unreliable")
            return None
        self._last_clock = now
        return now

    def _withdraw(self, status: str, reason: str) -> None:
        if self._current is not None or (self._status, self._reason) != (status, reason):
            self._revision += 1
            self._render_token = f"render-{self._revision}"
        if self._current_opportunity is not None and self._current_opportunity["outcome"] is None:
            self._current_opportunity["outcome"] = status
            self._current_opportunity["reason"] = reason
            self._current_opportunity["withdrawn_host_monotonic_ms"] = self._last_clock
        self._current = None
        self._current_opportunity = None
        self._status, self._reason = status, reason

    def ingest(self, event: CoachEvent) -> bool:
        """Accept one strictly newer source event, otherwise hide the old card."""
        now = self._now()
        if now is None or self._closed:
            return False
        error = _validate_event(event)
        if error is not None or event.stream_id != self.stream_id:
            self._counters["rejected_inputs"] += 1
            self._withdraw("unknown", error or "stream_mismatch")
            return False
        if (event.sequence != self._last_sequence + 1 or event.frame_received_ms <= self._last_frame
                or event.frame_id in self._frame_ids):
            self._counters["rejected_inputs"] += 1
            self._withdraw("unknown", "nonadvancing_source")
            return False
        if event.source_sequence is not None and (self._last_source_sequence is not None
                                                   and event.source_sequence <= self._last_source_sequence):
            self._counters["rejected_inputs"] += 1
            self._withdraw("conflict", "nonadvancing_source_sequence")
            return False
        if event.status == VISIBLE and event.source_kind != "synthetic" and event.source_sequence is None:
            self._counters["rejected_inputs"] += 1
            self._withdraw("unknown", "source_sequence_unknown")
            return False
        if event.emitted_ms > now or now >= event.expires_ms:
            self._counters["rejected_inputs"] += 1
            self._withdraw("stale", "stale_candidate")
            return False
        self._last_sequence, self._last_frame = event.sequence, event.frame_received_ms
        if event.source_sequence is not None:
            self._last_source_sequence = event.source_sequence
        self._frame_ids.add(event.frame_id)
        self._last_source_ingress = now
        self._counters["accepted_events"] += 1
        if event.status != VISIBLE:
            self._withdraw(event.status, event.reason)
            return True
        if event.advice is not None and event.advice.advice_id == self._last_advice_id:
            self._counters["rejected_inputs"] += 1
            self._withdraw("conflict", "reused_advice")
            return False
        if event.advice is not None:
            self._last_advice_id = event.advice.advice_id
        self._current, self._status, self._reason = event, VISIBLE, "candidate"
        self._revision += 1
        self._render_token = f"render-{self._revision}"
        opportunity = {
            "opportunity_id": f"{event.stream_id}:{event.sequence}",
            "source_stream_id": event.stream_id,
            "source_sequence": event.source_sequence,
            "source_kind": event.source_kind,
            "frame_id": event.frame_id,
            "frame_received_ms": event.frame_received_ms,
            "source_emitted_ms": event.emitted_ms,
            "source_expires_ms": event.expires_ms,
            "context_version": event.context_version,
            "evidence_id": event.evidence_id,
            "evidence_status": event.evidence_status,
            "input_host_monotonic_ms": now,
            "session_id": self.session_id,
            "revision": self._revision,
            "render_token": self._render_token,
            "outcome": None,
            "reason": None,
            "withdrawn_host_monotonic_ms": None,
            "paint_report_received_host_monotonic_ms": None,
            "input_to_paint_report_received_ms": None,
        }
        self._opportunities.append(opportunity)
        self._opportunities = self._opportunities[-256:]
        self._current_opportunity = opportunity
        return True

    def source_fault(self, reason: str = "source_unavailable") -> None:
        """Fail closed without inventing a source frame or sequence number."""
        if type(reason) is not str or reason not in {"source_unavailable", "invalid_input", "source_silence"}:
            reason = "source_unavailable"
        self._withdraw("disconnected", reason)

    def tick(self, *, source_timeout_ms: int | None = None) -> None:
        """Apply local expiry/silence only; never manufacture a source event."""
        now = self._now()
        if now is None:
            return
        if self._current is not None and now >= self._current.expires_ms:
            self._withdraw("expired", "candidate_expired")
            return
        if (self._current is not None and type(source_timeout_ms) is int and 100 <= source_timeout_ms <= 10_000
                and self._last_source_ingress is not None and now - self._last_source_ingress >= source_timeout_ms):
            self.source_fault("source_silence")

    def reject_input(self, reason: str = "invalid_input") -> None:
        self._counters["rejected_inputs"] += 1
        self.source_fault("invalid_input" if reason else "source_unavailable")

    def report_browser(self, payload: object, *, source_timeout_ms: int | None = None) -> bool:
        """Record browser display milestones without allowing reports to revive a card.

        The browser's ``performance.now()`` is a separate clock domain.  The
        local input-to-report duration retained below is a transport and
        double-rAF opportunity observation, not device-screen or human-visible
        latency.
        """
        # A report cannot make a card current again after host-side expiry.
        self.tick(source_timeout_ms=source_timeout_ms)
        now = self._now()
        if now is None or type(payload) is not dict:
            return False
        expected = {"session_id", "client_id", "client_event_sequence", "revision", "render_token",
                    "opportunity_id", "stage", "visibility", "browser_monotonic_ms", "dom_to_raf_ms"}
        if set(payload) != expected:
            return False
        if (not _identifier(payload["session_id"]) or not _identifier(payload["client_id"])
                or not _identifier(payload["render_token"]) or not _identifier(payload["opportunity_id"])
                or not _time(payload["client_event_sequence"]) or payload["client_event_sequence"] < 1
                or not _time(payload["revision"]) or type(payload["stage"]) is not str
                or payload["stage"] not in {"dom_updated", "paint_opportunity", "withdrawn"}
                or type(payload["visibility"]) is not str or payload["visibility"] not in {"visible", "hidden"}
                or not _finite_number(payload["browser_monotonic_ms"], 10_000_000_000)):
            return False
        dom_to_raf = payload["dom_to_raf_ms"]
        if payload["stage"] == "paint_opportunity":
            if not _finite_number(dom_to_raf, 10_000):
                return False
        elif dom_to_raf is not None:
            return False
        if payload["stage"] == "paint_opportunity" and payload["visibility"] != "visible":
            return False
        report_id = (payload["client_id"], payload["client_event_sequence"])
        if report_id in self._browser_report_ids:
            self._counters["browser_reports_historical"] += 1
            return False
        self._browser_report_ids.add(report_id)
        current = self._current_opportunity
        fresh = bool(current and payload["session_id"] == self.session_id
                     and payload["revision"] == self._revision and payload["render_token"] == self._render_token
                     and payload["opportunity_id"] == current["opportunity_id"])
        self._browser_reports.append({"session_id": payload["session_id"], "client_id": payload["client_id"],
                                      "client_event_sequence": payload["client_event_sequence"], "render_token": payload["render_token"],
                                      "stage": payload["stage"], "visibility": payload["visibility"],
                                      "opportunity_id": payload["opportunity_id"], "revision": payload["revision"],
                                      "browser_monotonic_ms": payload["browser_monotonic_ms"], "dom_to_raf_ms": dom_to_raf,
                                      "received_host_monotonic_ms": now, "fresh": fresh})
        self._browser_reports = self._browser_reports[-512:]
        if len(self._browser_report_ids) > 512:
            self._browser_report_ids = {(row["client_id"], row["client_event_sequence"]) for row in self._browser_reports}
        if not fresh:
            self._counters["browser_reports_historical"] += 1
            return False
        self._counters["browser_reports_accepted"] += 1
        if payload["stage"] == "paint_opportunity" and current["paint_report_received_host_monotonic_ms"] is None:
            current["paint_report_received_host_monotonic_ms"] = now
            current["input_to_paint_report_received_ms"] = now - current["input_host_monotonic_ms"]
        return True

    def report(self) -> dict:
        self.tick()
        return {
            "schema_version": "human-coach-report-v1", "simulation": False,
            "host_clock_domain_id": self.clock_domain_id, "session_id": self.session_id,
            "source_authenticated": False, "strategy_verified": False, "execution_eligible": False,
            "counters": dict(self._counters), "opportunities": [dict(item) for item in self._opportunities],
            "browser_reports": list(self._browser_reports),
            "timing_note": "input_to_paint_report_received_ms is local input acceptance to a returned double-rAF opportunity report; it is not device-screen or player-visible latency.",
        }

    def view(self) -> dict:
        now = self._now()
        if now is None:
            return self._view(None, 0)
        self.tick()
        remaining = 0 if self._current is None else self._current.expires_ms - now
        return self._view(self._current, remaining)

    def _view(self, event: CoachEvent | None, remaining: int) -> dict:
        advice = None
        observation = None
        if event is not None:
            if event.advice is not None:
                advice = {"id": event.advice.advice_id, "category": event.advice.category,
                          "title_zh": event.advice.title_zh, "action_zh": event.advice.action_zh,
                          "reason_zh": event.advice.reason_zh}
            if event.observation is not None:
                observation = {"id": event.observation.observation_id, "title_zh": event.observation.title_zh,
                               "facts_zh": event.observation.facts_zh, "unknown_zh": event.observation.unknown_zh}
        return {
            "schema_version": "human-coach-view-v1",
            "stream_id": self.stream_id,
            "host_clock_domain_id": self.clock_domain_id,
            "session_id": self.session_id,
            "revision": self._revision,
            "render_token": self._render_token,
            "status": self._status,
            "reason": self._reason,
            "visible": event is not None,
            "remaining_ms": remaining,
            "lease_ms": min(800, remaining) if event is not None else 0,
            "source_kind": None if event is None else event.source_kind,
            "opportunity_id": None if self._current_opportunity is None else self._current_opportunity["opportunity_id"],
            "source_sequence": None if event is None else event.source_sequence,
            "frame_id": None if event is None else event.frame_id,
            "context_version": None if event is None else event.context_version,
            "evidence_id": None if event is None else event.evidence_id,
            "evidence_status": None if event is None else event.evidence_status,
            "source_authenticated": False,
            "strategy_verified": False,
            "execution_eligible": False,
            "advice": advice,
            "observation": observation,
        }


def replay_example_events(stream_id: str, *, start_ms: int = 1_000) -> list[CoachEvent]:
    """Fully fictional offline card fixtures, then one withdrawal."""
    return [
        CoachEvent(SCHEMA, "synthetic", stream_id, "fixture-example-alpha", 1, start_ms,
                   start_ms + 20, start_ms + 2_000, VISIBLE, "candidate",
                   CoachAdvice("fixture-advice-alpha", "positioning", "虚构例子 · 站位检查",
                               "若虚构前排条件成立：比较前后排位置",
                               "无来源夹具；实时对象、强化和棋盘均未知。")),
        CoachEvent(SCHEMA, "synthetic", stream_id, "fixture-example-population", 2, start_ms + 2_100,
                   start_ms + 2_120, start_ms + 4_000, VISIBLE, "candidate",
                   CoachAdvice("fixture-advice-population", "lineup", "虚构例子 · 人口检查",
                               "有虚构可用单位时考虑补满人口，再比较羁绊", "无来源夹具；阶段、空位与单位身份未知。")),
        CoachEvent(SCHEMA, "synthetic", stream_id, "synthetic-synergy-example", 3, start_ms + 4_100,
                   start_ms + 4_120, start_ms + 6_000, VISIBLE, "candidate",
                   CoachAdvice("synthetic-advice-synergy", "lineup", "假设演示 · 羁绊补位",
                               "若场上有棋子甲和棋子乙、备战有棋子丙且有空位：尝试补齐虚构羁绊甲",
                               "版本未核；若要替换其他棋子，先核对会失去的羁绊。")),
        CoachEvent(SCHEMA, "synthetic", stream_id, "synthetic-equipment-example", 4, start_ms + 6_100,
                   start_ms + 6_120, start_ms + 8_000, VISIBLE, "candidate",
                   CoachAdvice("synthetic-advice-equipment", "equipment", "假设演示 · 装备合成与携带者",
                               "若有暴风大剑和反曲之弓：巨人杀手可给后排物理携带候选霞",
                               "版本未核；若前排更缺装备或配方/携带者不符，先保留再比较。")),
        CoachEvent(SCHEMA, "synthetic", stream_id, "synthetic-transition-example", 5, start_ms + 8_100,
                   start_ms + 8_120, start_ms + 10_000, VISIBLE, "candidate",
                   CoachAdvice("synthetic-advice-transition", "economy", "假设演示 · 阵容转型缺口",
                               "若为凑虚构羁绊乙需替换虚构羁绊甲：先比较失去的羁绊和现有战力",
                               "版本未核；转型前还需确认换入棋子、替换对象与可承受的金币代价。")),
        CoachEvent(SCHEMA, "synthetic", stream_id, "fixture-example-withdrawn", 6, start_ms + 10_100,
                   start_ms + 10_120, start_ms + 12_000, "unknown", "unknown", None),
    ]


def _page(views: list[dict]) -> str:
    """Static no-network page. `textContent` avoids rendering card text as HTML."""
    payload = json.dumps(views, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'>
<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; connect-src 'none'\">
<title>玩家教练短提示演示</title><style>
:root{{color-scheme:dark;font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;background:#0d1420;color:#edf4fb}}body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;box-sizing:border-box}}main{{width:min(560px,100%);border:1px solid #344c65;border-radius:18px;background:#142131;padding:22px;box-shadow:0 18px 50px #0008}}.tag{{color:#a8c3dc;font-size:13px}}#state{{margin:14px 0;padding:14px;border-radius:12px;background:#0c1723;border:1px solid #35536d}}.warn{{border-color:#9c7f47!important;color:#ffe6ad}}h1{{font-size:20px;margin:0 0 4px}}h2{{font-size:26px;margin:4px 0}}.action{{font-size:20px;color:#8ed6ff;margin:8px 0}}.reason{{color:#c0d0df;margin:0}}.empty{{font-size:20px;color:#ffdaa0;margin:0}}button{{font:inherit;background:#275a82;color:#fff;border:1px solid #6ca6cf;border-radius:9px;padding:8px 14px;margin-right:8px}}button:disabled{{opacity:.45}}footer{{font-size:12px;color:#a8b9c9;margin-top:16px;line-height:1.55}}</style>
<main><div class='tag'>离线虚构示例播放 · 玩家自己操作 · 不连接平板</div><h1>金铲铲教练短提示</h1><div id='state' aria-live='polite'></div><p><button id='previous'>上一状态</button><button id='next'>下一状态</button><button id='play'>自动演示</button><span id='position' class='tag'></span></p><footer>全部内容均为虚构演示，不等于实际识别、版本策略或实时局面。未知、冲突、画面变化或到期会隐藏旧提示。页面没有真实断流监听，当前离线播放不声称已实测断流撤回。</footer></main>
<script>(()=>{{const views={payload},categories={{lineup:'羁绊/阵容',economy:'转型/经济',equipment:'装备',positioning:'站位',selection:'选择'}};const state=document.querySelector('#state'),previous=document.querySelector('#previous'),next=document.querySelector('#next'),play=document.querySelector('#play'),position=document.querySelector('#position');let index=0,playTimer=null,expiryTimer=null,countdownTimer=null,expired=false;function el(tag,text,klass){{const node=document.createElement(tag);node.textContent=text;if(klass)node.className=klass;return node}}function clearExpiry(){{if(expiryTimer!==null){{clearTimeout(expiryTimer);expiryTimer=null}}if(countdownTimer!==null){{clearInterval(countdownTimer);countdownTimer=null}}}}function activeView(){{const source=views[index];return expired?{{...source,visible:false,status:'expired',reason:'candidate_expired',advice:null}}:source}}function render(){{const source=views[index],view=activeView();state.replaceChildren();state.className=view.visible?'':'warn';if(view.visible){{const label=source.source_kind==='synthetic'?'假设演示 · 调用方虚构条件':'回放条件例子 · 调用方回放声明',remaining=el('div','有效期剩余：'+Math.ceil(view.remaining_ms/1000)+' 秒','tag');remaining.id='remaining';state.append(el('div',label+' · '+categories[view.advice.category],'tag'),el('h2',view.advice.title_zh),el('p',view.advice.action_zh,'action'),el('p',view.advice.reason_zh,'reason'),remaining);}}else{{state.append(el('p','提示已撤回：'+view.reason,'empty'),el('div','不沿用上一条动作、理由或数字。','tag'));}}position.textContent=`${{index+1}} / ${{views.length}}`;previous.disabled=index===0;next.disabled=index===views.length-1}}function scheduleExpiry(){{clearExpiry();const source=views[index],scheduled=index;if(!source.visible||source.remaining_ms<=0)return;const deadline=performance.now()+source.remaining_ms;const update=()=>{{if(index!==scheduled||expired)return;const remaining=Math.max(0,deadline-performance.now()),node=document.querySelector('#remaining');if(node)node.textContent='有效期剩余：'+Math.ceil(remaining/1000)+' 秒'}};countdownTimer=setInterval(update,100);expiryTimer=setTimeout(()=>{{if(index!==scheduled)return;expired=true;clearExpiry();render()}},source.remaining_ms);update()}}function select(nextIndex){{index=nextIndex;expired=false;render();scheduleExpiry()}}function stopPlay(){{if(playTimer!==null){{clearInterval(playTimer);playTimer=null;play.textContent='自动演示'}}}}previous.onclick=()=>{{stopPlay();if(index)select(index-1)}};next.onclick=()=>{{stopPlay();if(index+1<views.length)select(index+1)}};play.onclick=()=>{{if(playTimer!==null){{stopPlay();return}}select(0);play.textContent='停止演示';playTimer=setInterval(()=>{{if(index+1>=views.length){{stopPlay();return}}select(index+1)}},1800)}};select(0)}})();</script></html>"""


def _private_output(path: Path) -> Path:
    if not path.is_absolute() or path.exists() or path.is_symlink():
        _fail("output_path_invalid")
    try:
        parent = path.parent.resolve(strict=True)
        info = parent.stat()
    except OSError:
        _fail("output_parent_invalid")
    if (parent == PROJECT or PROJECT in parent.parents or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or info.st_mode & 0o077):
        _fail("output_parent_invalid")
    return parent / path.name


def _write_new(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def build_demo(output: str | Path) -> dict:
    """Write one standalone replay-example page outside the repository."""
    target = _private_output(Path(output))
    target.mkdir(mode=0o700)
    demo_clock = [1_000]
    store = HumanCoachStore(stream_id="demo-player-coach", clock=lambda: demo_clock[0])
    views = []
    for event in replay_example_events(store.stream_id):
        demo_clock[0] = event.emitted_ms
        if not store.ingest(event):
            _fail("synthetic_demo_rejected")
        demo_clock[0] = event.emitted_ms + 1
        views.append(store.view())
    page = target / "player-coach-demo.html"
    report = target / "report.json"
    _write_new(page, _page(views).encode("utf-8"))
    _write_new(report, json.dumps({"schema_version": "human-coach-demo-report-v1", "simulation": True,
                                   "source_kinds": sorted({view["source_kind"] for view in views if view["source_kind"] is not None}),
                                   "mode": "manual_replay_example",
                                   "execution_eligible": False,
                                   "views": views}, ensure_ascii=False, indent=2).encode("utf-8"))
    return {"page": str(page), "report": str(report), "frames": len(views), "simulation": True}
