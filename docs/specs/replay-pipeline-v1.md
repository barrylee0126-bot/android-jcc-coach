# 回放纵向桥接 v1

实现：[jcc_replay_pipeline](../../src/jcc_replay_pipeline/)。它是无 I/O、无图片持久化的窄适配器：调用方把一条视觉机器候选和一份项目外私有帧清单交给它；它只产生 25D 的局部观察请求，以及 25E 可接受的“仅观察”事件。它不读取平板、录像路径、账号、坐标或游戏资料，不启动浏览器，也不生成点击。

## 外层帧清单与边界

每个调用必须显式提供一份精确的私有清单项：`case_id`、`frame_id`、源视频 SHA-256、编码 PNG SHA-256、真实解码 `decode_sequence`，以及二选一的 PTS：

- `{"kind":"integer_timebase","ticks":...,"time_base_num":...,"time_base_den":...}`；或
- `{"kind":"seconds_text","value":"..."}`，该字符串须是调用方保留的无损时间文本。

桥接器逐项核对视觉顶层、人口和每条羁绊行的 case、帧、源视频摘要与编码原帧摘要。不一致就拒绝，不能把 B 帧识别结果显示为 A 帧。`decode_sequence` 只能来自视频逐帧解码记录；不得由文件名、展示 `sequence` 或浮点 PTS 猜测。

清单的 PTS 由外层调用方声明。旧视觉合同里仅有的浮点 `decoded_pts_s` 只能做非认证诊断，绝不被反推成整数 tick 或原始秒字符串。所有成功绑定固定标记 `pts_evidence=external_manifest_caller_declared` 与 `provenance_authenticated=false`：哈希相符、同像素或函数回执都不代表本模块已认证媒体来源。

25D 现有局部合同只接受最多九位小数的秒文本。如果清单是整数 PTS/time base，或 `seconds_text` 本身超过该精度，适配器绝不截断或四舍五入为伪“原始秒”；会将 25D 的 `pts_seconds` 送为 `unknown`，并只在私有 `replay_binding` 保留完整 ticks/time base 或原字符串。这个降级不影响其他同帧字段作为机器候选显示，但不能升级来源认证。

视觉合同的 `decoded_pts_s` 仍只是浮点诊断。来自**同一次 25B 输出**的顶层、人口和行必须在 `Decimal(str(float))` 后完全相同；不一致即拒绝。顶层与外层整数 tick/time base 仅允许至多 `0.000001` 秒的差值：这是 25B 冻结旧输出以六位小数序列化、再与 tick 换算比较的误差上限，不是同帧证明；超出即拒绝。该检查不会认证来源、恢复原始 tick、把相同像素当作同一时刻，或声称子毫秒同一性。调用方应把未通过检查的画面作为未知并按展示端控制合同撤回。

## 下游观察与撤回

完整显示行才可产生 `display-row-<row_id>`，这是显示行键，绝不是游戏羁绊目录 ID。行整体、名称和数量必须同为机器候选且绑定同一帧；名称或数量任一未知时，行不会进入下游。未知人口不变成零。本人、阶段、备战棋子、金币、规则绑定和候选棋子维持 `unknown`。

桥接在同一受控调用中运行 25D，回执摘要覆盖完整私有清单（包括源/帧摘要、PTS 对象和真实解码序号）及由本桥接生成的完整局部请求，并逐字段核对 25D 结果与原请求。A 帧回执不能喂给 B 帧事件；对 `partial_request` 或“可执行/已验证/准确率”等边界字段的事后修改也会拒绝。摘要不是密钥或来源认证：即使调用方重算摘要，25E 消费边界仍会重新验证 `active_traits` 的结构、类型、长度和算术关系，并将畸形内容受控拒绝。这仍然不是来源认证。回放事件从清单派生原始 `source_sequence`，不会让调用者另传或拿展示 `sequence` 冒充它。`source_kind=replay` 映射为 25E 支持的 `replay_candidate`，标题/上下文 ID 标为“回放”；`synthetic` 保持 `synthetic`，标题/上下文 ID 标为“合成”。JSONL 的 `coach_event` 严格只含 25E 合同字段，私有 `replay_binding` 必须分开保存。

任一不一致、无效或过期结果都不得显示旧观察。没有新的源帧时，桥接**不构造**复用旧 `frame_id` 的撤回 JSONL 事件；调用方必须按 25E 的本地 `tick(source_timeout_ms)` 或 `source_fault(...)` 控制合同撤回。只有实际新失效帧到来，才能以新的帧 ID 和连续展示序号写入失效事件。

## 运行

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  -m unittest discover -s src/jcc_replay_pipeline/tests -v
```

测试只使用虚构的清单、摘要和视觉合同形状，覆盖原帧摘要、跨帧人口/羁绊错配、浮点时间冲突、未知残留、整数及超精度文本 PTS 的不降精度、受控请求/边界字段篡改、25D 回执替换和 25E JSONL 形状。通过只说明桥接合同；不证明视觉准确率、媒体真实性、策略质量、浏览器/平板可见性或实战效果。
