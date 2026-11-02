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
