# 24D 全分母时延审计 v1

`jcc_latency_audit` 是一个只读的私有机会日志审计器。它不连接平板、不读取图像、不调用 OCR、不作游戏决策，也不发送触控。它的用途是把一次可观察的机会从画面观察到识别、决策、操作、回读或提示显示的时间点统一记录，并让未知、超时、撤回和失败与成功机会留在同一分母。

## 日志合同

私有 JSONL 的每一行都是一次机会，固定字段如下；日志不应包含截图、OCR 原文、设备标识、账号、坐标或游戏内名称。

```json
{"schema_version":"latency-attempt-v1","opportunity_id":"opaque-1","source_kind":"replay","execution":"none","outcome":"unknown","observed_monotonic_ns":1000000000,"capture_received_ns":1010000000,"recognition_completed_ns":null,"decision_completed_ns":null,"action_sent_ns":null,"feedback_received_ns":null,"display_reported_ns":null}
```

- `source_kind` 只能是 `synthetic`、`replay` 或 `live`；它保留来源，不会把回放改标成实机。
- `execution` 是 `none`、`simulated` 或 `attempted`。只有 `attempted` 可记录 `action_sent_ns` 或 `feedback_received_ns`；动作时间点本身尚未埋点时，可以只记录已确认的反馈。
- `outcome` 为 `advice_displayed`、`action_confirmed`、`unknown`、`timeout`、`withdrawn` 或 `failed`。前两者分别要求已经记录显示或动作回读；其余终态允许在任一前段中止。
- 时钟值必须是同一主机单调时钟域中的纳秒，且不得早于 `observed_monotonic_ns`。可选阶段留空代表本机会未测，不代表零毫秒或成功：例如早期实机可以只记录“观察→动作→回读”或“观察→显示”，不必伪造识别或决策时间。显示支路和动作/回读支路可并行，但每一条支路会移除未测节点后核验所有剩余端点非递减，不能借由空阶段接受已记录的逆序时间。

每个 `opportunity_id` 只能出现一次。写日志的一方必须为每一次机会写终态，即使没有识别候选、没有动作或已被撤回。

## 统计与边界

输出只在同一个 `source_kind/execution` 组内计算每段 `p50`、`p95`、最小和最大值，绝不将 replay、synthetic、live 或模拟/真实操作合成一个时延数。每段只在两个端点都有记录的子集上计算，同时输出该组完整机会分母和未测数量；不足 20 个样本时 `p95_ms=null`。这避免用成功候选的局部耗时隐瞒未知、超时或撤回。

`all_supplied_rows_accounted` 仅表示本次提供给审计器的非空行都有一个终态；它不证明日志生产者没有遗漏机会。报告固定写 `input_opportunity_completeness=not_verified_by_audit`。空日志的该布尔值为 `false`。

该模块仍不能测量平板物理采集到主机、物理屏幕真实可见、人眼反应、识别准确率、决策质量或上分效果；这些字段在报告中显式保留为未测。

## 运行与验证

输入和报告应在项目外私有目录；报告必须是新路径并以 0600 创建。报告路径的父目录会解析并核验，最终名称则不做解析；任何已存在的文件或软链接（包括断链）都会拒绝，避免跟随软链接写出。JSONL 重复键也会拒绝。命令不会访问平板：

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m jcc_latency_audit \
  --attempts /absolute/private/attempts.jsonl \
  --report /absolute/private/latency-summary.json

PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  -m unittest discover -s src/jcc_latency_audit/tests -v
```

24E 后续实机探索可只记录匿名机会和阶段时间点交给此模块；首次报告必须将 `source_kind=live` 与实际可操作、回读成功、未知和停止原因分开，不能把现有回放统计改写为实机端到端速度。`source_kind` 是调用方声明，审计器只能保留并分组，不能自行证明素材来自实机。

## 25H 真实提示可见性合同

为未来的“平板局面变化→玩家能看到提示”联调，使用另一种固定行结构 `latency-visibility-attempt-v1`。它与原 v1 并存；原 v1 不会被改写。每行仍是一项机会，必须包含一个终态，即使被未知、遮挡、断连、超时或撤回终止。

```json
{"schema_version":"latency-visibility-attempt-v1","opportunity_id":"opaque-1","record_kind":"frame_opportunity","source_kind":"live_candidate","source_stream_id":"opaque-stream","source_sequence":81,"execution":"none","outcome":"browser_raf_reported","host_clock_domain_id":"host-run-opaque","host_stream_started_ns":null,"case_id":null,"case_partition":null,"source_video_digest":null,"source_frame_digest":null,"source_pts_ns":null,"source_capture_clock_domain_id":null,"source_capture_ns":null,"replay_input_read_started_host_ns":null,"host_frame_received_ns":1000000000,"recognition_completed_ns":1050000000,"decision_completed_ns":1060000000,"overlay_ingested_ns":1070000000,"browser_dom_report_received_ns":1080000000,"browser_raf_report_received_ns":1090000000,"browser_withdrawn_report_received_ns":null,"presentation_session_id":"overlay-session","presentation_revision":7,"render_token":"opaque-render-7","target_client_id":"target-client","dom_report_session_id":"overlay-session","dom_report_client_id":"target-client","dom_report_client_event_sequence":18,"dom_report_revision":7,"dom_report_render_token":"opaque-render-7","dom_report_opportunity_id":"opaque-1","dom_report_stage":"dom_updated","dom_report_visibility":"visible","dom_report_browser_monotonic_ms":1200.0,"raf_report_session_id":"overlay-session","raf_report_client_id":"target-client","raf_report_client_event_sequence":19,"raf_report_revision":7,"raf_report_render_token":"opaque-render-7","raf_report_opportunity_id":"opaque-1","raf_report_stage":"paint_opportunity","raf_report_visibility":"visible","raf_report_browser_monotonic_ms":1205.0,"withdrawn_report_session_id":null,"withdrawn_report_client_id":null,"withdrawn_report_client_event_sequence":null,"withdrawn_report_revision":null,"withdrawn_report_render_token":null,"withdrawn_report_opportunity_id":null,"withdrawn_report_stage":null,"withdrawn_report_visibility":null,"withdrawn_report_browser_monotonic_ms":null,"browser_dom_to_raf_ms":4.5,"physical_visible_clock_domain_id":null,"physical_visible_ns":null,"user_acknowledged_clock_domain_id":null,"user_acknowledged_ns":null}
```

`host_frame_received_ns` 是主机收到完整画面的时间，不是平板传感器或显示时刻。`recognition_completed_ns`、`decision_completed_ns`、`overlay_ingested_ns`、浏览器 DOM/RAF 报告的**服务器接收**时间都在同一主机单调时钟域才可以相减。浏览器的 `browser_dom_to_raf_ms` 是浏览器本地的间隔，单独统计，不能与主机绝对时间相加。

`source_capture_*`、`physical_visible_*` 和 `user_acknowledged_*` 是成对可选的外部时钟点。只有其 clock domain 和主机域完全相同才计算时延；同域时还要求采集不晚于主机收帧、物理可见/确认不早于主机收帧。不同域记录为 `cross_clock_not_compared`，绝不相减。因而双 RAF 回报至多证明浏览器已执行相应渲染机会并把回报送达主机，不能证明平板屏幕已发光或玩家已经看见。`user_acknowledged` 也只是后续外部观察记录，不能倒推首次可见。

`record_kind=frame_opportunity` 的行必须保留匿名的 `source_stream_id`、`source_sequence` 和 `source_kind`，使采集、识别、决策和提示组件能对齐同一次机会；不要使用设备号、账号或原始画面标识。`stream_start_failure` 是独立的无帧分母：只记录 stream、主机时钟、启动时间和 `disconnected/timeout/failed`，所有帧、识别、提示和案例字段都必须为空。它不伪造序号，也不声称无帧期间的真实游戏机会数量已知。

浏览器端的 DOM/RAF POST 可以并行发送，因此两种**服务器接收**时间都只是 `overlay_ingested_ns` 之后的独立分支，不要求 DOM 回报先到。两条回报各自绑定，不能把一个事件序号同时充当 DOM 和 RAF：

- DOM 回报字段为 `dom_report_session_id/client_id/client_event_sequence/revision/render_token/opportunity_id/stage/visibility/browser_monotonic_ms`，其中 `stage=dom_updated`，`visibility` 可为 `visible` 或 `hidden`。
- RAF 回报字段为对应的 `raf_report_*` 字段，要求 `stage=paint_opportunity` 且 `visibility=visible`；任何 hidden RAF 载荷都会拒绝，不能以 `unknown` 等终态混入绘制样本。`browser_raf_reported` 终态还要求该完整匹配回报。`browser_dom_to_raf_ms` 只可与完整匹配的 RAF 回报一起记录，必须是 0–10000 的有限非负毫秒值；浏览器本地 `browser_monotonic_ms` 不和主机绝对时间相减。
- 撤回回报字段为 `withdrawn_report_*`，要求 `stage=withdrawn`，与可选的 `browser_withdrawn_report_received_ns` 配对；它保留撤回终态，不会被计为 RAF 成功或 DOM→RAF 样本。

任一实际 DOM/RAF/撤回回报都必须逐项与期望的 `presentation_session_id/revision/render_token/target_client_id/opportunity_id` 相等，并携带正的本客户端事件序号；同一客户端事件序号在整个输入批次内只能出现一次，同一声明的实际事件也不能被绑定到两个不同源帧。若同一行同时有 DOM 和 RAF，浏览器事件序号必须 DOM 严格早于 RAF，浏览器本地单调时间必须 DOM 不晚于 RAF；这条规则不限制两条 POST 到达服务器的先后。仅有期望绑定而没有实际浏览器回报的 `timeout`、`expired`、`disconnected` 等终态合法，且绝不计入 DOM/RAF 时延成功样本。一个输入批次只审计一个 `target_client_id`，包含只有期望绑定的超时行，避免把 Mac 的绘制机会记到平板；跨客户端对照不在本版范围内。同一 `(session, revision, token, target_client)` 不得为不同源帧计 RAF 成功。

`visibility` 是发送时 `document.hidden` 的快照，既不是卡片几何可见，也不是屏幕物理显示或玩家实际看到。即使 DOM 或双 RAF 报告字段逐项相等，检查也只验证调用方提供的字段；报告的 `evidence_boundary.browser_binding` 明确该匹配未经认证，不能认证浏览器、服务或玩家端来源。

`replay/replay_candidate` 必须完整绑定 `case_id`、`case_partition`（`development` 或 `holdout`）、源视频与帧的 SHA-256 摘要、实际 `source_pts_ns`；`live/live_candidate/synthetic` 必须将这些 case 字段全部置空。PTS 只用于定位，绝不写入采集时钟或与本次主机时钟相减；若要量回放本次读取，只能使用 `replay_input_read_started_host_ns`。下列任一精确相同的标识跨 `development/holdout` 都会拒绝：`case_id`、`(source_video_digest, source_pts_ns)`、`source_frame_digest`。相邻相似帧、不同编码但像素相同的帧是否重复，仍未由该审计器判断，不能借分区声明独立样本。

终态为 `browser_raf_reported`、`unknown`、`occluded`、`disconnected`、`timeout`、`expired`、`superseded`、`withdrawn` 或 `failed`。前者要求有主机收到的、完整逐字段匹配的 visible `paint_opportunity` RAF 回报；其余终态允许在任何已测阶段结束。来源和时钟域均为调用方声明，审计器仅做结构、同域顺序和全分母检查，不能自证 live、时钟同步、物理显示或玩家看到。

命令同样不会触碰平板：

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m jcc_latency_audit \
  --visibility-attempts /absolute/private/visibility-attempts.jsonl \
  --report /absolute/private/visibility-summary.json
```
