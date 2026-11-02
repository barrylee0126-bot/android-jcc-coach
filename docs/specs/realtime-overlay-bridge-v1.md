# Realtime overlay bridge：v2 迁移与合成联调

开发15B，2026-11-02。文档路径保留 `realtime-overlay-bridge-v1.md`，实现及报告已升级到 v2。依赖 [10I/10J 的 realtime-probe-v2 合同](realtime-probe-v1.md) 与 [14D 的 timed-overlay-event-v1 合同](timed-overlay-v1.md)。

## 当前结果与边界

15B当时只完成合成生产→增量 JSONL→桥接→原计时消费端→电脑浏览器模拟页面，未访问真实回放。23B随后新增本机离线回放与实际OCR，范围和结果见下文；仍不访问Android或输出正式策略。

## 23B：连续离线回放与本机显示

实际 OCR 需要 macOS、Pillow 和 `tools/setup-macos` 从已收录的 Swift 源码编译的 Apple Vision 工作程序；合成回归无需该程序。离线回放还需自行准备私有帧清单和区域配置，本仓库不提供本局画面或配置。

入口读取项目外的私有逐帧清单、ROI配置并新建项目外0600报告：

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_overlay_bridge replay \
  --manifest /path/to/private/frames.jsonl --profile /path/to/private/profile.json \
  --report /path/to/private/new-report.json --port 18765 --start-delay 5
```

清单每行包含从1连续递增的`source_sequence`、从0开始严格递增的`received_offset_ns`和位于清单目录内部的相对`image_file`；上限240帧、120秒。配置使用原探针的`image_size`、`input_boxes`和恰好一个`gold`数值区域；坐标和画面均不应放进公开仓库。帧在本次回放间隔到达时才从磁盘读取。`LatestFrameStream`、原像素连续性检查、一个活动工作及一个可替换等待位、桥接和原模拟网页均复用，帧来源为replay，本次读帧及处理时钟绑定同一个进程的`time.monotonic_ns`。浏览器只收到匿名事件，不接收画面、OCR文字或数值。

对每帧实际调用本机OCR并做1×/2×候选一致性及已有字形检查。任何未知、冲突或空结果都不产生ready；过期、断源、新帧或像素依赖变化按已有屏障撤回。ready只显示固定的“模拟计时 · 仅测试显示”，不是现金数值、决策或实际建议；主体、场景、遮挡、游戏规则和合法动作均未验证。新增`ocr_unresolved`及主机时钟回放为v2增量扩展，严格枚举旧合同的消费者须先升级。

2026-11-02本机运行：从既有录像同一约10秒片段按2帧/秒离线抽取20帧，构成原尺寸画布，仅保留指定金币ROI原像素，其余为黑色。按500ms间隔回放；前处理、历史视频解码、原始录制/平板采集不计入回放计时。本机Chrome实际运行网页并报告：全部20帧完成检查及OCR，7帧出现可展示的数字开发候选，10帧有文字但候选未解、3帧无有效文字，13帧保持无结果；正式字段0。7/20帧有及时DOM回报，7/20帧有及时双`requestAnimationFrame`绘制机会回报，最终网页提示撤回。该分母是本次20个开发帧，不是准确率，也不是独立验收数据。

| 时段或计量 | 结果 |
| --- | --- |
| 本机读已处理画布 | 20次中位0.22ms |
| 收帧后像素检查 | 20次中位27.31ms |
| OCR工作 | 20次中位55.49ms；首次220.35ms，之后19次中位55.11ms |
| 本机收帧到处理完成 | 20次中位80.87ms；首次258.56ms，之后19次中位80.80ms |
| 本机文件读取开始至DOM回报到达服务 | 7次中位169.61ms，范围117.57–311.49ms |
| 本机文件读取开始至绘制机会回报到达服务 | 7次中位173.11ms，范围120.21–326.07ms |

各行的分母和时间区间不同，不应相加；7个回报不足以计算P95，也不能忽略其余13帧。浏览器回报包含请求和返回传输；双RAF是绘制机会，不证明物理屏幕刷新或用户看到，实际可见和实战端到端时延均未知。冷/热指同一OCR工作进程的首次与后续，不表示磁盘冷缓存。首轮实际页面可见后，汇总因纳秒文本与整数相减而失败；第二轮本机处理通过但浏览器扩展来源检查拒绝，故无浏览器回报；上述表格只取第三轮完整报告。没有为测试启动对局或访问平板。

15B 交付时，桥接62项、消费端49项Python和9项Node测试通过；当时上游66项中一个旧兼容测试的两个子例仍断言桥接必须拒绝v2，因而失败。该旧断言后来已迁移，不代表当前失败。2026-11-02 的10L合成回归中，设备层193项、实时探针84项、桥接62项均通过；该回归没有重新运行消费端测试，也没有访问平板或真实录像。

10L 补充修复了 Python 3.11 中关闭等待被 SIGINT 打断后，线程库可能提前报告线程已停止的情况。接收任务退出和数据源关闭尝试完成由独立信号记录；`receiver_thread_alive` 在这里表示接收任务仍待完成，不作为操作系统线程存活的精确测量。源关闭报错仍单独报告。隔离测试进程使用受控内存源发送真实 SIGINT，覆盖关闭等待、外部线程等待和启动清理中的再次中断；这不构成设备实测。

独立生产进程运行时消费事件并通过真实HTTP观察状态；浏览器一轮10个机会，4个及时绘制机会回报，其余6个失败/无结果/替代全部保留。另有2张内存生成图的实际本机Apple Vision调用，实际调用2次、有效文字项1项；不是游戏准确率测量。正式字段、正式建议、玩家实际可见时延仍未验收。

## 运行入口

```sh
# 5帧，可控时钟；没有OCR调用或设备访问
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_overlay_bridge synthetic

# 运行中的合成子进程→桥接→HTTP消费服务
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_overlay_bridge stream

# 原消费端网页，2轮共10个合成机会，自动结束
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_overlay_bridge serve --port 18765 --seconds 25 \
  --cycles 2 --start-delay 3 --processing-ms 20

# 桥接回归，含OCR适配器替身和故障注入
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B \
  -m unittest discover -s src/jcc_realtime_overlay_bridge/tests -v
```

`--report /absolute/path/new-run.json`新建报告并拒绝覆盖。`stream/serve`只启动固定的本机合成生产子进程，没有设备、真实文件或任意外部时钟参数。`--processing-ms`为人为等待，不是OCR性能；多轮须提供足够的`--seconds`，不足会记未通过。依赖现有虚拟环境及Pillow，不安装新依赖。

## v2 接收及来源

接收固定18键的`realtime-probe-v2`，不接受v1、缺字段或额外字段，不凭旧`processor`字符串补来源。生产者自身Schema若不再是v2，桥接拒绝装配。`EventSink`必须显式给`processing_kind`；模拟通过`simulated_report()`完成，不再构造虚拟OCR项，也不使用旧`failed=`参数。

| 上游字段 | 桥接口径 |
| --- | --- |
| `source_kind` | 保留像素来源；synthetic→synthetic、replay→replay、live→device_readonly。分类不证明实际连接了该来源。 |
| `processing_kind` | 固定绑定本轮simulated_work或ocr，独立于像素来源。每行均检查。 |
| `processing_outcome` | 保留未完成、运行中、三种模拟完成及四种OCR完成，不混写。 |
| `ocr_requests` | 完成事件报告的实际recognize调用数，包括失败；桥接不按输入框或范围数推算。 |
| `ocr_items_count` | 实际成功返回中通过10J结构校验的有效非空文字项；模拟及未完成为null。不是原列表长度或已确认文字。 |
| `ocr_errors` | 实际失败或非列表返回的调用数；调用前失败可以0次，模拟完成固定0。 |

用上游公开`ProcessingResult`校验完成组合，并另检查阶段/状态/原因的关系。未完成行必须三个计数均null；`processing/started`须为in_progress。模拟不能报实际candidate、OCR文字项、ocr_empty或ocr_failed；ocr_text须正数实际调用及有效项且无调用错误。相同作业在两个范围及后续撤回重复携带的计数必须一致。

来源/时钟使用上游公开`validate_source_clock()`：live只允许主机单调时钟，replay和synthetic允许虚拟或主机单调时钟。主机时钟回放表示本次读完帧的时间；允许组合仍须满足下述实际时钟绑定。

## 映射与独立审计

只给原消费端发送固定十字段`timed-overlay-event-v1`，不扩展或伪造正式字段。当前pending样本的`pixel_inputs/simulated_candidate`和`pixel_inputs/candidate`都能触发一次**模拟ready**，原始状态分别保留在`ready_from_status`和审计中。只有pixel_full完成不触发ready。

- `field_gate/no_output`与`field_gate/rejected/semantic_evidence_missing`独立记账，不提前终结模拟处理，不产生正式字段。
- 模拟无输出、模拟失败、实际空结果、未执行OCR、调用前错误、部分成功后失败及10F/10G捕获错误保留原完成结果。像素已撤销与ocr_empty可以同时存在。
- 旧样本结果/撤回只留审计，不恢复旧提示、不撤掉更新样本。新帧先调用公开`store.source_fault("bridge_new_frame")`建立屏障，再发pending；不使用会包含新帧的invalidated水位。
- `upstream_audit`保存所有通过合同与样本绑定检查的原始匿名行，包括两个像素范围、field_gate、晚到结果和撤回。坏输入不回显。
- 每帧`processing_result`保留完成字段。`processing.completed_jobs_observed`按唯一sample_id计一次；`ocr_totals_observed`只加该次完成报告。范围事件另列`pixel_scope_event_counts`，不与作业或调用分母混用。
- `output_audit`保存十字段输出、关联上游event_index、处理结果快照及消费者接收结果。桥接本地产生的超时另有明确origin，关联上游event_index为null，不冒充上游新事件。

桥接没有独立观测OCR调用本身，`ocr_invocations_independently_observed_by_bridge=false`。测试以实际调用OCRProcessor及可计数替身/本机Vision来核对报告。存在开始而无完成报告的作业时，`ocr_requests_in_uncompleted_jobs=null`，不会把完成报告合计0解释成所有工作实际调用0次。

## 原期限、超时和序号

固定显示截止为`min(原始expires, 原始receive + receive_silence_limit)`，默认静默上限1000ms，原始结果TTL为2000ms。读取、处理完成、重复事件和浏览器回报均不能续期。精确边界`now >= deadline`失效。

1. pending到期时，在核实的共用时钟中用当前观测时刻签发timeout，仍保留原frame与显示截止；`origin=bridge_deadline_observed`。这是桥接观测时间，不改写上游emitted。
2. 有效的新接收行到桥接时已经过期，直接发送无pending的timeout，`origin=bridge_expired_on_arrival`。该帧仍计入桥接与消费分母，不把期限挪向未来。
3. 下游timeout在截止时/之后可接收。`pending seq1 → timeout seq2 → 新帧pending seq3`连续接收；若下游已自行到期，timeout只是历史确认，不重复计机会或覆盖首次结果。
4. 旧终态在桥接已校验后保留审计并过滤；不利用消费端的历史兼容性绕过上游序号/样本检查。重复、乱序、缺号、来源或时钟改变、上下游重启均撤回并封闭会话。

`receive/rejected/check_timeout`是同一帧的补发预算拒绝，不增加第二个机会。消费者即使返回接受ready，也可能只记超时；桥接再读取公开视图，避免把消费时跨截止误计ready。

## 时钟及增量传输

同进程绑定实际`EventSink`并与消费端共享同一个callable；主机时钟标签必须对应`time.monotonic_ns`。虚拟时钟不能与主机时间比较。

跨进程只接受本工具启动并暂停的本机合成子进程。专有管道核对随机挑战、真实PID、同次开机摘要、单调实现、父进程收发读数包围，以及v2事件Schema和simulated_work处理来源。消费者建立后才放行。握手为`bridge-local-clock-v2`；任意字符串域名、远端或旧握手不构成证明。此机制防误接，不是对同账户恶意代码的远程认证。

纳秒在Python中使用精确整数，报告按十进制字符串导出，覆盖超过`2**53`的测试。浏览器只读取消费端的有限毫秒时长。

JSONL有界增量解析，拒绝重复键、NaN、未知字段和超长行。部分行立即撤回，补全后不能恢复退役样本。EOF、受控空文件的身份变化、截断和已读前缀改写终止输入；不倒退重放。每会话最多1000个接受帧、20000个输入及输出事件；文件辅助最多8MiB。

## 完整分母与显示解释

`opportunities`是观察到的receive/received尝试数，包含被拒绝的尝试；`outcomes`总和必须等于它。重复尝试不能称为独立局面。坏JSON、断流及封闭后未观察的帧数无法推断，分别记录故障及null。

`accepted_frames`、消费者不同sample数、作业开始/完成/无报告、两个范围事件和OCR调用各有自己的分母。检查前拒绝的收帧可能使桥接/消费者分母不同，`accepted_frames_missing_consumer_sample`明确保留差额。上游10J上限外帧不产生receive事件；桥接仅凭事件无法得知其数量，`unchecked_source_frames=null`。运行器另保留生产者摘要，比对frames_checked/jobs_completed/OCR合计，不把邮箱替换或未检查丢弃混进检查机会。

显示另列consumer_ready_samples、timely_dom_report_samples、timely_paint_report_samples、ready_without_timely_paint_report及全部接收机会中未获及时绘制回报的数量。即使所有ready都回报，也必须同时报告总机会分母。

ready消费、DOM回报、双RAF绘制机会、截图和用户实际看到是不同证据。`user_saw_hint_at_ns`、`actual_end_to_end_visible_latency_ms`、设备变化到接收均保持null。主机同步撤回不保证浏览器在网络、主线程或系统暂停期间于物理截止瞬时清屏。

## 历史合成验证

早期浏览器联调的 10 个合成机会中有 4 次及时绘制机会回报，其余 6 次为无结果、替代或未完成；正式输出为 0。这些结果仅描述合成通路，不证明物理屏幕显示或实际用户可见。

实战接入、游戏字段准确率、建议价值、玩家可见总时延和上分能力均仍待验证。
