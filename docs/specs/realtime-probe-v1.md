# Realtime probe：只读新鲜度与分段计时（事件合同 v2）

开发10H，2026-11-02；10I 同日修复模拟事件来源，事件与报告版本改为 `realtime-probe-v2`；10J 随后修复有效文字项、硬帧数上限与来源/时钟矩阵；10K 修复断源发布竞争、启动失败清理及输出创建权限。本文件保留原路径以维持已有文档入口。实现位于 [jcc_realtime_probe](../../src/jcc_realtime_probe/)，通过 `PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe` 运行。复用 [10E 接收所有者与合并门控](vision-integration-v1.md)、[10G 同帧绑定与候选门控](scene-evidence-v1.md)、`LatestFrameStream`、`TabletStream` 和已有 Apple Vision；未修改共享 Schema、10E/10G 或正式策略。

## 目的与结果层次

本工具检查：在每张准入的权威接收帧均被检查、结果沿用原期限的前提下，完整 RGB 与实际 OCR 输入局部是否有不同的连续性；慢处理返回时是否仍可保留这一**像素证据**。硬上限外及关闭中的在途帧单独记为未检查丢弃，并使旧结果失效，不会跳过后继续使用旧段。它不提供游戏操作、字段真值、独立复核、正式建议或可见提示。

三层必须分别解释：

1. `pixel_full / pixel_inputs`：只比较收到的解码 RGB 字节。`processing_kind=ocr` 且实际 OCR 调用成功返回文字项时，才可发 `candidate`；它仍只表示声明的像素依赖未变，不输出文字、数字值或游戏意义。`processing_kind=simulated_work` 只能发 `simulated_candidate`，表示假设处理成功时像素票据仍有效；没有执行 OCR，也没有返回文字项。
2. 10G：对实际同一 `Frame` 调用 `probe_scene`；配置数字字段时再调用 `probe_numbers / assess_fields`，保留所有尝试输入。没有真实逐帧 `FrameWitness`，字段保持未知；配置中的名称、Agent 看图与 OCR 不是独立人员复核。
3. 10E：保留 `continuity_enabled=True`、真实校准与上下文未配置的安全路径。逐帧调用原 `observe_frame`，每个完成作业仍进入原 `publish` 审核。本工具不构造真实 `ContextEvidence`，不把局部像素票据交给正式合并，也不改写旧来源。

整帧场景识别器若仍读取整帧，其依赖仍是整帧。本工具的局部诊断不缩减那个合同，也不证明隐藏状态、己方归属、准备阶段或无遮挡。

## 接收、任务与撤回

`LatestFrameStream.bind_observer(core)` 在启动前绑定唯一观察者。共用原10E的可重入 `receive_lock`：分配权威 `FrameRef` → 硬帧数门控 → 10E逐帧审核 → 可选私有录帧 → 整屏及所有局部输入精确比较 → 撤回旧结果 / 更新待处理槽 → 进入最新帧邮箱。消费者可跳帧，准入帧的逐帧检查不能跳帧。

- `run_live` 将整数 `max_frames` 传给接收所有者 `ProbeCore`。所有者在同一接收锁内、10E审核和录制之前检查硬上限，保证检查数和录制数均不超过上限；控制线程的轮询仅负责停止。第一张上限外帧立即使所有者终止，撤回旧候选、丢弃待处理槽；活动作业迟到完成也不能恢复候选。接收回调不关闭或等待接收线程，外部控制线程沿用原关闭流程。
- 只有一个执行作业和一个可替换待处理槽。原接收序号不重编；最新帧邮箱替换与 OCR 待处理替换分别计数。
- 比较原始 RGB 字节，不设像素差容差、不使用感知哈希或只比末帧。单像素单通道变化即更换对应范围的代号。A→B→A 不恢复最早 A。
- 配置的 `input_boxes` 与所有10G线索、数字输入框自动组成完整依赖集合；没有命中的阻断线索等输入也不删除。局部外的变化可以不撤回**局部像素**，但不证明语义上下文没变。
- 重复、乱序、序号缺口、时间非法、错流、坏图像、尺寸不符、alpha/调色板、未应用EXIF旋转、预算超限或输入静默均撤回。序号缺口帧未知，后续连续有效帧只能开新段。换流、时钟倒退和断开为终止状态。
- 进程内票据绑定原始 `FrameRef`、私有字节摘要、两个变化代号、原截止及对象所有者。摘要只防混图；连续性比较仍逐字节完成。票据不序列化，不升级成10C/10E上下文证明。
- 运行中轮询、每次新帧和作业完成均重新检查。已有像素候选遇新变化、静默、到期、替换或断开会产生一次撤回/过期事件；复制出去的旧对象仍须由消费方自行按期限失效。

10K 将观察者所有断开调用纳入同一可重入接收锁，包括源读错误、接收回调错误、在途停止、接收线程 `finally`、显式关闭及接收线程构造/启动失败。已通过票据检查的完成作业若先持锁，则完成发布后由断开路径撤回；断开若先持锁，则迟到完成只能被拒绝。断开不能在检查与发布之间改写 ledger 后留下未撤回候选。需要两把锁时统一为 `receive_lock → 邮箱 _condition`；显式关闭先在邮箱锁内设置停止并唤醒消费者，释放邮箱锁后才等待接收锁和线程。启动失败也先释放邮箱锁，再通知观察者和关闭源。

`run_live` 的装配和两条线程启动均在清理保护内。原始源进入后暂由装配栈拥有，`LatestFrameStream` 构造成功后在保护内交接；此后只由流关闭，启动成功时仅接收线程关闭原始源，不再由外层上下文并发或重复关闭。尚未启动的 OCR 线程不执行 `join`；已启动的线程先请求停止，再等待退出。OCR 后端由专属资源栈拥有，超时提前关闭时消耗同一关闭回调，退出时不会再次关闭。启动异常仍向调用方传播，普通 `Exception` 在 CLI 中只输出类型；源只有恢复性 `TimeoutError` 时沿用无首帧的有界截止，结束为 `no_frames`，不把它报告为成功完成。

固定复用10E工程参数：来源接收后 **2000ms 结果期限、1000ms 接收存活、250ms 检查预算**，均未做真实校准。CLI不提供延长这些期限的参数；即使不断收到相同新帧，也不延长旧结果。精确边界 `now >= deadline` 即失效。事件签发再次核对期限，跨边界的两类候选均改记过期。事件写入导致的晚预算失败另外发 `receive/rejected/check_timeout` 并清空待处理槽，不能将初次receive事件当可用字段。

检查预算包含10E检查、可选录帧、真实RGB解码/比较与本次接收回调工作。预算是工作返回后的检查，不能强杀卡住的同步解码、文件系统或外部回调。继承的设备/管道缓冲和未采样中间帧不在证明内。

## 运行方式

以下合成命令不连接平板、不启动OCR、不读取真实资产：

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe synthetic --case outside_motion
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe synthetic --case inside_aba
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe synthetic --case sequence_gap
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe synthetic --case receive_stall
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe synthetic --case static --processing-ms 2000
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_realtime_probe/tests -v
```

合成默认20张160×100 RGB图、250ms接收间隔及模拟1450ms处理；`--processing-ms` 是模拟输入，不能作为 Apple Vision 实测速度。模拟完成报告使用 `ocr_requests=0 / ocr_items_count=null / ocr_errors=0`，不制造一个虚构 OCR 项来驱动候选。虚拟计时器在没有新帧时也触发原截止和接收静默；接收与完成恰好同刻时先检查新帧。

真实只读入口必须显式使用 `tablet`，经项目现有设备配置选择默认平板。采集仍使用 `--no-control --no-window --no-power-on`，不唤醒、不触控、不匹配：

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe tablet --profile /path/to/private/profile.json \
  --duration 20 --max-frames 80 \
  --events /path/to/private/new-live.events.jsonl \
  --report /path/to/private/new-live.report.json \
  --record-private /path/to/private/new-recording
```

私有配置必须显式给出真实解码尺寸和完整输入框，不自动按比例、旋转、黑边或设备推断。下面仅为**生成图的配置示意**，不能直接用于游戏：

```json
{
  "image_size": [160, 100],
  "input_boxes": [[10, 10, 70, 40]],
  "cues": [],
  "numeric_fields": []
}
```

可选 `cues` 使用10G固定 `cue_id`、`box`、`exact_texts`、`scale`；`numeric_fields` 使用10F固定 `field_name`、`box`、`scale`、`syntax`。程序校验并将所有真实尝试输入加入依赖。未配置场景/数字时分别保持布局未知/未配置；不能为非对局页面假配金币、血量或阶段。

运行配置限制为60秒、整数1–240张的硬检查/录制上限、最多32个输入框、单图25MiB/1600万像素；实时默认目标4 fps。`policy.max_checked_frames` 报告该上限。到限或停止时，采集仍可能返回在途帧；这些帧不录制、不进入逐帧检查、不发出 `receive/received`，也不延长旧结果。

结束报告满足 `stream.frames_received = counts.frames_checked + stream.frames_discarded_unchecked`。`counts.frames_declined_unchecked` 是接收回调实际拒绝的数量；关闭时在观察者回调之前被 `LatestFrameStream` 丢弃的在途帧仅计入总的 `stream.frames_discarded_unchecked`。原 `stream.frames_discarded / frames_replaced / frames_delivered` 是最新帧邮箱的独立账目，其中可以包含已检查或未检查帧，不能再与未检查丢弃数相加充当互斥帧分类。真实检查数可能少于上限；不靠重复启动补足数量。源关闭、作业关闭、错误类型及未完成作业均报告。

真实图像不默认落盘。`--record-private` 显式启用后，只存到项目外新目录（0700），每张准入接收图及 `frames.jsonl` 为0600；包括已准入但未用于OCR的帧，上限外丢弃帧不录制。清单保存原接收序号及相对主机接收间隔，不包含设备ID或OCR内容。输出文件、配置和清单均拒绝位于本项目内，拒绝覆盖已有输出。

10K 对 CLI 的 `--events / --report` 采用以下创建合同（不改变录帧格式或输入读取接口）：

- 在创建系统调用中直接指定 `0600`，配合 `O_CREAT | O_EXCL | O_NOFOLLOW`；进程 umask 只能进一步收窄权限，没有先以0644/0666创建再 chmod 的窗口。两个参数各自必须是新文件；相同路径、已有普通文件、目录、有效或悬空的最终符号链接一律失败，不修改其内容或权限。
- 父目录必须已存在。先解析父目录真实路径并检查位于项目外，再通过目录句柄固定它，以相对文件名独占创建；最终文件名不先 resolve，因而不能经悬空链接另建目标。父目录别名允许指向项目外目录，指向项目内的别名被拒绝。这不提供对不可信同用户进程或祖先目录所有者的完整文件系统隔离。
- 包装文本句柄失败时关闭原始文件描述符；第二个输出创建失败、处理失败和写入/关闭异常时仍尝试关闭所有已打开输出。异常只报告类型。成功内容只在输出关闭后打印；文件内容没有成功标记，调用方还须检查退出状态。
- 失败保留本轮已创建的0600空文件或部分文件，供检查；不按路径 unlink，不对路径 chmod，也不回滚或覆盖任何已有文件。若路径在运行中被用户替换，清理只关闭本轮已持有的句柄，不删除或改权限于替代文件。已有其他进程持有的历史文件描述符不受本次修复追溯影响。

按真实接收间隔回放已录像素：

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_realtime_probe replay --manifest /path/to/private/recording/frames.jsonl \
  --profile /path/to/private/profile.json --processing-ms 1450 \
  --events /path/to/private/new-replay.events.jsonl \
  --report /path/to/private/new-replay.report.json
```

`replay` 使用录下的像素和原接收间隔/序号，**本次作业时间为模拟值**；新的虚拟主机时间不是历史游戏时间，也没有再次运行真实OCR。即使图像确实来自真实录像，其事件仍为 `source_kind=replay / processing_kind=simulated_work`，只可产生 `simulated_candidate`。清单中的序号缺口原样保留。同样输入/模拟时间/测试时钟域产生相同事件；解码检查的实测耗时可以不同。

## 匿名 JSONL 事件合同

`EventSink` 只接受固定代码与自行生成的匿名运行标识；不接受任意文案。下面是**虚拟合成事件**示意：

```json
{"schema_version":"realtime-probe-v2","event_index":4,"clock_domain_id":"aaaaaaaaaaaaaaaa","clock_kind":"virtual_monotonic_ns","sample_id":"aaaaaaaaaaaaaaaa:1","source_kind":"synthetic","frame_received_monotonic_ns":10000000000,"emitted_monotonic_ns":11450000000,"expires_at_monotonic_ns":12000000000,"stage":"pixel_inputs","status":"simulated_candidate","reason":"simulated_pixel_inputs_only","evidence":"pixel_only_not_game_state","processing_kind":"simulated_work","processing_outcome":"simulated_ready","ocr_requests":0,"ocr_items_count":null,"ocr_errors":0}
```

| 字段 | 约定 |
| --- | --- |
| `schema_version` | 固定为 `realtime-probe-v2`。不与 v1 兼容，不把旧行改标为 v2。 |
| `sample_id` | 随机运行前缀+真实接收序号。同一来源帧的各阶段使用同一个标识；没有账号、设备或原样本映射。 |
| `source_kind` | 像素来源为 `synthetic / replay / live`，不互相改标；此字段不能证明是否执行 OCR。 |
| `processing_kind` | 每条事件必填，固定为本轮的 `simulated_work / ocr`，与像素来源分别声明。 |
| `processing_outcome` | 完成前为 `not_completed`，工作开始行为 `in_progress`。完成后为 `simulated_ready / simulated_no_output / simulated_error` 或 `ocr_text / ocr_empty / ocr_unresolved / ocr_not_run / ocr_error`。`ocr_unresolved` 是返回有效文字但候选校验未通过；撤回、替代、过期和完成后的 field_gate 行保留该作业的完成结果。 |
| `ocr_requests` | 完成前为 `null`。完成后为实际进入 OCR 适配器 `recognize` 的调用次数，包括失败的调用；不是配置框数、估算值或底层 Vision IPC 次数。模拟固定为0。 |
| `ocr_items_count` | 实际 OCR 成功返回中通过结构校验的非空文字项累计数，包括部分成功调用的有效项。只验证文本、分数和本次输入内的框，不验证文字含义；模拟固定为 `null`，完成前也为 `null`。不得用返回列表长度或10F/10G的原始诊断项数代替。 |
| `ocr_errors` | 实际失败或返回非列表的 `recognize` 调用次数，完成前为 `null`，模拟完成固定为0。图像预处理失败可能发生在首次 OCR 调用之前，此时三项实际计数均为0，结果为 `ocr_error`。 |
| `frame_received_monotonic_ns` | live为Mac读取到完整JPEG后的 `time.monotonic_ns()`；位于等待所有者锁之前，不能改写成检查/完成时刻。 |
| `emitted_monotonic_ns` | 同域签发时刻，在写JSONL之前取点；不是消费、浏览器DOM或可见时刻。 |
| `expires_at_monotonic_ns` | 始终等于本帧原接收+2000000000；所有阶段、拒绝与撤回均保持原值。撤回行允许签发晚于原截止。 |
| `clock_kind / clock_domain_id` | 按下表核准的来源/时钟组合。随机域标签只标识这一轮，不能独立证明跨进程同机同次开机。 |
| `event_index` | 本轮成功写入事件的严格递增序号，与来源帧序号不同。 |
| `stage` | `receive / queue / processing / pixel_full / pixel_inputs / field_gate`。 |
| `status` | `received / started / candidate / simulated_candidate / rejected / expired / withdrawn / no_output`；两类候选仅限两个像素阶段。`candidate` 必须匹配 `ocr / ocr_text` 且有正数实际调用及返回项；`simulated_candidate` 必须匹配 `simulated_work / simulated_ready`。 |
| `reason / evidence` | 固定原因码；`evidence` 固定为 `pixel_only_not_game_state`。 |

每张检查帧均有 `receive/received` 和 `field_gate/no_output`；不只发送成功项。待处理替换/丢弃、工作开始、两个像素范围的工作结果，以及语义拒绝均分别记录。像素结果行必须携带完整处理结果，不允许凭状态或旧的 `processor` 文案猜测来源。

来源与时钟支持矩阵：

| 像素来源 | `host_monotonic_ns` | `virtual_monotonic_ns` |
| --- | --- | --- |
| `live` | 支持，只读运行器使用此组合 | 拒绝 |
| `replay` | 支持：23B 同进程离线回放使用本次本机读完帧的时间，绝不是录制或设备采集时间 | 支持，保留原间隔/序号，以虚拟时间运行 |
| `synthetic` | 支持，用于受控的本机生产者、实际OCR接口实验及15A独立进程实验 | 支持，用于确定性合成时间线 |

像素来源、时钟和处理来源分别声明；synthetic使用主机时钟不会变成live，也不能冒充实际OCR。`EventSink` 在构造和每次写入前调用公开的 `validate_source_clock()`。消费方须核对同一矩阵，再核实共用时钟或受控本机进程证据；允许组合本身不是时钟证明。现有15A的 `bind_same_process()` 仍会拒绝主机时钟标签配虚拟callable。原探针CLI的synthetic/replay仍使用虚拟时钟；新增实际OCR的离线回放入口见[23B桥接](realtime-overlay-bridge-v1.md)。

有效文字项必须是字典，`text` 为去除首尾空白后非空的字符串；`x/y/width/height/confidence` 为非布尔的有限数值，分数处于 `[0,1]`，框有正面积且完全处于**本次实际OCR输入**内，包含10F/10G放大后的尺寸。空文本、缺项、坏框、NaN/Infinity等均不增加有效项数。混合列表只累计有效项，但整个原列表原样交给10F/10G，其多项歧义、框边缘、分数阈值和字段见证门槛不变；不能通过过滤坏项使正式字段更容易通过。

实际调用成功但没有有效文字项为 `no_output/ocr_empty`，包括只返回无效项的列表；10F/10G辅助报告仍保留原始诊断项数及拒绝原因。没有调用为 `no_output/ocr_not_run`；调用失败或返回非列表为 `rejected/ocr_failed`，调用前或后处理错误为 `rejected/processing_failed`。模拟无输出或失败分别使用 `simulated_no_output / simulated_failed` 原因码。10F/10G 内部捕获的 OCR 异常也进入实际调用与错误计数；即使此前已返回部分有效文字项，整个完成结果仍为 `ocr_error`，不得产生候选。异常只输出类型，不回显原始异常、路径、子进程日志或OCR原文。

23B 增加 `no_output/ocr_unresolved`：至少一次成功OCR和一个有效文字项，但数字多尺度结果不一致、不完整或字形检查不通过；保留实际调用与文字项数，禁止将其冒充空OCR或候选。该原因码是v2的增量扩展，严格枚举旧码的其他消费者须同步升级；旧消费者应拒绝未知码并撤回，不可降级成ready。

`ocr_empty` 与证据已变化可以同时发生；事件的拒绝原因保留像素/时序原因，`processing_outcome=ocr_empty` 和实际计数仍保留，消费者无需查报告才能区分二者。处理失败优先记录失败原因。不能把每范围结果、OCR 调用次数、作业数与帧数混为同一分母。

已签发的两类候选在失效时发 `withdrawn` 或 `expired`，替换原候选另发 `superseded`，并保留原完成结果的处理来源及计数。消费者必须按 `sample_id + stage` 处理撤回，重读时复核原截止和接收存活；不得用读文件时间续期。输入全程单写者、同域串行化；事件不含图像、摘要、裁切坐标、原OCR/数值、设备标识、私有路径、版本资料或正式建议。

这是10H的实验事件，**不是14B的十字段 ready/pending 合同**。后续由总控协调独立桥接，保留像素来源、处理来源、拒绝、原期限和全分母；live来源或实际 OCR 也只可驱动明确的模拟显示。虚拟事件不可直接交给真实时钟服务；旧live事件重放也已失效。跨进程须核实实际生产者/主机/时钟，不能只复制域标签。

## 10I 合同迁移、15B 衔接与重新联调

10I/10J交付时未修改15A，当时只接受 v1 的桥接会拒绝 v2；该结论保留在原交接中作为历史边界。15B随后显式迁移到v2。10K等待15B会话明确idle后才修改共享上游，未改15B接口或源码；上游的旧“v2必须被当前桥接拒绝”断言已改为检查实际v2处理来源、计数及仅模拟显示。以下迁移要求继续有效：

1. 桥接的固定字段、版本与状态校验须显式接受 v2 以及新增五个处理字段。禁止删除新字段、将版本改回 v1 或把 `simulated_candidate` 简单改名为 `candidate`。
2. `EventSink` 构造必须显式传入 `processing_kind`。模拟生产者通过 `simulated_report()` 提供完成结果，不再使用虚构 `ocr_items_count`；`ProbeCore.complete(job, report)` 不再接收独立 `failed` 标志，失败由同一完成结果声明。
3. 模拟显示映射需分别处理 `simulated_candidate` 与实际 OCR 的 `candidate`，并在桥接的审计记录/报告中保留处理来源、结果与计数。14B仍为固定十字段协议，不能通过静默丢弃新字段而失去来源证据。
4. 重新覆盖 synthetic/replay/live 的像素来源与独立处理来源组合、实际空结果、调用前错误、部分失败、10F/10G 捕获错误、两个像素范围及正式字段拒绝。模拟作业进入10E时 `SceneEvidence.ocr_items_count=0`，不得传入虚构文字项；实际 OCR 失败的部分文字也不交作成功证据。
5. 重跑原期限边界、新帧撤回、迟到/重复/乱序、静默、部分 JSONL、独立进程时钟与浏览器显示/清空及完整分母，生成新的 v2 证据；旧 v1 证据仅作为历史基线。

10I 的修复范围、验证要点及限制见上述合同。

10J在同一v2结构上收紧有效文字项与接收上限，未增加JSONL字段。15B须持续使用有效项定义及上述来源/时钟矩阵，保留synthetic+主机时钟的受控进程路径，并把检查帧分母与上限外未检查丢弃分开。10K复跑当前桥接、消费端和页面回归以及运行中的合成生产/消费链路；这不等于实机或实际用户可见性验收。

## 计时和证据解释

报告版本为 `realtime-probe-v2`，顶层保留 `processing_kind / processing_outcome_counts / ocr_totals`。`ocr_totals` 只合计实际调用、实际返回项和调用错误；模拟三项均为0，但每个模拟作业的 `ocr_items_count` 仍为 `null`，不能解释成执行了空 OCR。

报告按接收检查、私有录帧、排队、处理、收帧至处理完成、像素事件签发、10E门控分别给出纳秒样本和统计；首项与其余项另列。`receive_to_emit` 包含full/inputs两个范围、两类候选的事件，可能每作业两项，不能当作独立帧数。模拟处理计时仍是模拟输入，不能当作实际 OCR 速度。P95为最近秩经验值，少于20项留空；少量样本的P95不是稳定性承诺。

`source_timeouts / consumer_timeouts` 是有界轮询空等，不自动等于丢帧。明确分别列出接收帧、逐帧检查、邮箱替换/关闭丢弃、OCR槽替换、作业开始/完成/未完成、拒绝、过期和无输出。ffmpeg的fps采样可能在上游丢帧或重复帧；上游数量未知，不能声称检查了所有设备物理帧。

设备变化→采集→传输缓冲、提示DOM/绘制/实际可见和玩家动作均未测，保留为 `null`。正式字段输出为0；无可信真值、准确率和可操作决策机会分母。这是可复现链路实验，不能用于声称实战速度、准确率、响应目标达标、谁操作更快或上分效果。
