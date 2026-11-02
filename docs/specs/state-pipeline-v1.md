# State pipeline v1：最新帧与局面契约的只读联调

开发 10B / 10E，2026-11-02。实现为 [`state_pipeline.py`](../../src/jcc_device/state_pipeline.py)，依赖已交付的 [game-state-v1](game-state-v1.md)、[vision-stream-v1](vision-stream-v1.md) 和 [vision-stability-v1](vision-stability-v1.md)。Schema 和语义规则仍以它们指定的真源为准。10E 的接收线性化、证据前提和边界见 [vision-integration-v1](vision-integration-v1.md)。

本模块完成来源、尺寸、上下文、未知字段、异步结果屏障和有效期的最小接通。没有准备阶段字段识别器，没有五格商店识别器，也没有模式或补丁核验器。唯一的正向视觉规则是**默认关闭、未校准的历史页面 OCR 实验**。合成测试不能证明真实场景识别正确。

## 1. 入口与输出

在项目根目录执行，使用项目环境；不安装依赖、不触控、不唤醒、不启动匹配：

```sh
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source synthetic
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source synthetic --experimental-history
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source tablet --observations 3 --experimental-history
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source synthetic --state-only
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source synthetic --experimental-history --continuity synthetic --synthetic-delay-seconds 1.45
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source tablet --observations 3 --experimental-history --continuity unconfigured
```

`--source synthetic` 使用内存生成的纯色 JPEG；启用历史实验时还使用固定的假 OCR。它只演示接口，不是从图片读出了游戏文字。`sample_kind=synthetic` 和逐条 `synthetic_ocr=true` 会明确标识。真实平板使用项目包装器选择默认设备，沿用 `TabletStream(capture_diagnostics=False)` 的 `--no-control --no-window --no-power-on`。

新增 `--continuity disabled|unconfigured|synthetic`，默认 `disabled` 完整保留原保守路径。`unconfigured` 接通权威逐帧钩子，但没有校准和上下文证据，结果保持未知；`synthetic` 仅为生成图提供人工构造的校准/上下文夹具，并在打开任何设备前拒绝与 `--source tablet` 合用。固定合成上下文不是真实游戏的已核场景。`--synthetic-delay-seconds` 只给合成假 OCR 增加等待，不适用于平板。

默认输出 JSON 报告，关键成员如下：

| 成员 | 语义 |
| --- | --- |
| `single_frame_observations` | 每次任务的来源帧、时钟、上下文、尺寸、场景观察、固定证据代码、OCR 条数和实际处理时间。它是单帧观察记录，**不是 game-state-v1，也不是运行时当前值**。 |
| `runtime_before_close` | 捕获仍在运行时读取的状态摘要；晚到结果可能全部被拒绝，即使该帧本身有观察结果。 |
| `runtime_before_close_state` | 同一时刻完整、经校验的 `game-state-v1`；用于复核来源帧/锚帧和原始期限。保存后仍只是当时快照。 |
| `state` | 退出捕获后经 Schema 和语义校验的完整 `game-state-v1`。关闭已使可用值失效，不能把这份文件作为仍在线的缓存。 |
| `pipeline_stats/capture_stats/processor_stats` | 接纳、失效、发布/拒绝、取帧、替换、处理及清理计数；不代表识别正确数。 |
| `validation` | `schema_and_semantics_passed`，或首帧未到时的 `no_frame_no_state`。首帧前不制造来源不明的局面。 |

`--state-only` 只向标准输出写最终状态，方便送入既有校验器；没有首帧时输出 `null` 并以非零退出。正常退出也会撤销值，所以历史实验的最终场景可能是 `stale`，而不是 `recognized`。使用模块 API 的运行中消费者应调用 `snapshot()`。

`--scene-ttl-ms` 默认 2000 毫秒；这是**未校准的实验参数**，不是经过实测确认的安全窗口。策略代号包含该值及 `uncalibrated`，同一实例中不可修改。`--timeout-seconds` 默认为 15，限制等待观测的循环；OCR、线程和画面源退出还需各自的有界清理时间，报告的 `total_seconds` 包含清理，因此可能超过这个参数。

异常报告只含类型；不输出或保存图像、设备标识、账号、昵称、OCR 原文或原始子进程日志。脚本不会创建样本文件。项目原有传输层仍使用临时私有 FIFO；它不是录屏文件。

## 2. 来源与时间

`stream_id` 直接来自本次 `LatestFrameStream.stream_id`；`frame_id` 和 `sequence` 直接来自 `FrameRef`，跳号不会补造帧。`host_clock_id` 是当前 Python 进程生成的随机时钟域代号；测试可显式注入合成时钟及代号。重新连接须新建接收器、管线及源适配器，不复用旧流。

帧接收时间严格取 `FrameRef.host_received_monotonic_ns / 1_000_000`，不取重新处理时的时间，不使用 UTC 进行排序。`observed_host_monotonic_ms` 与引用帧的接收时间相同。`completed_host_monotonic_ms` 保留实际处理完成时间；结果如果早于已发布的同帧未知屏障则直接拒绝，不把完成时间挪后来绕过校验。字段判断完成、来源接收及快照组装均处于同一时钟域。

实际尺寸由 Pillow 解码该帧获得。没有额外旋转，所以 `rotation_applied_degrees=0` 指本模块未施加旋转，不声称知道平板硬件方向。`game_viewport` 和 `calibration_id` 为 `null`，不把全屏猜作已校准的游戏区域。实验 OCR 框从像素坐标转为来源整帧的归一化坐标；未知商店不生成区域或点击目标。

`device_capture_time=null`：未测平板采集或真实显示时间。接收点位于主机拿到完整 JPEG 之后。默认旧适配器先撤销旧结果，再由接收器打时间戳；连续性路径直接读取原源，立即记录字节接收时间，再在共同锁内分配 `FrameRef`、检查和入槽，因此检查耗时在该时间之后。scrcpy、ffmpeg、系统管道和设备内的积压仍未知。`receive_to_finish_seconds` 只能解释为**主机应用收帧到判断完成**，不能当作平板变化到识别、提示或操作的完整延迟。

## 3. 状态所有者与接收屏障

默认 `--continuity disabled` 的数据流保留为：

```text
TabletStream（或合成源）
  → InvalidatingFrameSource：每个完整非空字节帧先撤销旧输入
  → LatestFrameStream：分配真实 FrameRef，单槽保存最新帧
  → StatePipeline.advance：解码尺寸、分配新上下文、发布全未知屏障
  → FrameProcessor(SceneRecognizer)：最多一个正在执行的任务
  → StatePipeline.publish：核对身份、顺序、时间、有效期并校验
  → StatePipeline.snapshot：运行时再次检查有效期
```

`InvalidatingFrameSource` 必须在接收器启动前绑定一次，并由这一个 `LatestFrameStream` 独占读取。适配器对每个合法非空字节帧的计数与接收器序号一致。它在把帧交给最新槽之前调用 `note_ingress(sequence)`，立即封闭旧输入；这样“已收到新 JPEG，但主线程尚未取走它”的间隙也不能重新发布旧结果。通知只撤销已有状态，不提前伪造新帧尺寸或接收时间。

`advance(frame)` 拒绝外流、序号倒退、未来接收时间、接收时间倒退及落后于已通知接收序号的帧。解码失败会撤销旧值，不为坏图像填写猜测尺寸。每个接纳帧都先输出完整八个核心字段和五格商店的未知封套。

当前没有已校准的局部变化检测器，无法证明任意两帧之间仍是同一可视上下文。因此**每个接纳帧都分配新的 `context_epoch`**；它是保守的失效代数，不是已识别的场景切换次数、赛季或帧序号。丢失/替换的帧不会生成假上下文。几何、方向、裁切变化自然得到新代数。外部知道弹窗或布局失效时可调用 `invalidate_context()`：立即封闭当前任务；下一个真实帧才建立新的上下文及元数据。

所有更新由同一个带锁的状态所有者串行提交。每次提交均运行 `validate_state(candidate, previous=accepted)`，通过后原子替换，递增修订号。未知、识别、失效和断流都沿用同一校验路径。公共返回值是深拷贝，消费者改写它不会改变已接纳状态。

可选连续性路径的数据流为：

```text
原始源 → LatestFrameStream 在 StatePipeline.receive_lock 内分配真实 FrameRef
  → StatePipeline.observe_frame：每个序号先验证上下文、RGB 连续性和实际尺寸
  → 同一状态所有者更新锚帧/字段屏障并校验
  → 最新单槽（只有到这里才允许丢帧/替换）
  → advance 选择已经检查的当前帧 → 原有单任务执行器
  → publish 重新验证票据、原 TTL、字段水位、冲突与完整契约
```

`LatestFrameStream.bind_observer(pipeline)` 只能在启动前绑定一次，与旧 `InvalidatingFrameSource` 二选一。分配帧身份、检查和入槽全程持有与 `publish/snapshot` 相同的可重入锁；已分配的新帧检查尚未结束时，旧结果发布及状态读取均等待。原始源读取不持锁；尚未被这个有序接收者分配身份的底层字节不在应用连续性证明内。跳号不会补造，乱序不会忽略，中间 B 帧不会被 A→B→A 的末帧覆盖掉检查。

只有显式 `RegionCalibration`、逐帧 `ContextEvidence`、相关与上下文区域完全相同且未超检查预算时，场景组才保留旧字段来源。历史识别器读取整帧 OCR，故其相关 ROI 的并集必须覆盖**整幅图像**，不能仅提供最终标题框。当前没有真实游戏校准或上下文/遮挡验证器，实机默认不能提供这些前提。失败帧更新未知水位并打断内部上下文段；解码失败不猜尺寸，只撤销已有可用值，下一张可解码帧才能建立新锚点。

`publish(context, result)` 在保守路径仍只接纳当前 `FrameContext`。连续性路径允许同一已证明段的旧来源结果，但票据必须由本实例在权威接收帧上生成，证明末帧必须等于当前锚帧；完成时间、来源帧、观察时刻及原始截止时间不改写。仍逐一检查受影响字段的来源水位、同帧完成时间、未解决冲突，以及 `validate_state(candidate, previous=accepted)`；较新未知也能挡住旧结果。`frames` 保留锚帧、字段/候选引用的原帧；暂未引用的待识别来源在合并时只能按已证明的同代数几何恢复其原元数据。每个来源只接受**一个结果**，第二个结果不会静默覆盖；现有冲突须另行显式复核，不能通过场景单结果 API 解除，候选到期则撤为未知。

程序接口新增 `continuity_enabled=False`、`calibration=None`、`context_provider=None`、`max_frame_age_seconds=1.0`、`max_check_seconds=0.25`。后两个默认是未校准的工程参数。验证器应有界且不执行慢 OCR；超过检查预算、帧新鲜度界限或提供者异常均失去继承资格。Python 无法强杀卡住的提供者：卡住时发布/读取被锁挡住，接收器 `close()` 会在总关闭预算后明确抛出 `FrameShutdownTimeout` 并保留存活统计；提供者返回后仍会处理停止并撤销，不能冒充关闭成功。`FrameContext` 第五项新增可空的进程内票据，原四参数构造保持兼容。

`note_ingress` 只保护已进入本应用的完整帧；尚未从底层管道读出的画面变化仍不可见。本模块不能证明物理画面最新。

## 4. 字段与运行时有效期

| 条件 | 输出 |
| --- | --- |
| 未启用历史实验或证据不足 | `scene=unknown`；模式、版本、阶段、回合、血量、金币、等级、五格商店均未知。 |
| 历史实验的两类证据在同帧满足 | 仅 `scene=history/recognized`；阶段、回合、血量、金币、等级、商店为 `unknown/not_applicable`；当前模式、版本仍为未知。 |
| 默认路径新帧接收、或显式上下文失效 | 已有识别值变 `stale/value=null`，保留旧值及原始来源；旧任务封闭，依据旧场景派生的不适用状态撤销。 |
| 默认路径新帧接纳、或可选路径连续性未通过 | 全字段引用该新帧并恢复未知，不复制旧数值或区域；坏图像无法提供尺寸时仅撤销旧状态。 |
| 可选路径中逐帧精确检查同段通过 | 更新锚帧，保留场景及其不适用推断的原来源；未实现的模式与版本仍引用新帧未知。可在原期限内接纳同段待返回任务。 |
| 当前时间到达/超过截止时间 | 场景变 `stale/reason=expired`；原观察、完成、截止时间不变，旧值仅留在 `last_known_value`。派生的不适用状态撤销。 |
| 断流/停止 | 立即封闭任务并撤销当前可用值；无自动重连、无旧流沿用。 |

截止时间为 `来源帧接收时间 + scene_ttl_ms`，不是识别完成时间加 TTL。相同帧重算或晚返回不能续期。`snapshot()` 会使用当前同域时钟检查；已经取得的字典是一个时间点的快照，外部保存后仍须按实际读取时间检查有效期、来源域及捕获是否仍在线。

未知不等于 0、空商店或功能可用。本模块不输出可操作性观察、策略或操作目标。历史页面中的赛季、模式、名次及数值不进入当前局面。没有开发 07 的完整核验依据时，`game_version` 始终未知。

## 5. 历史页面实验与证据范围

实验只支持横屏、同帧两个不同布局线索：上半屏恰好一个固定历史标题，另一个固定历史辅助标签在它下方；存在已知弹窗标签或多个标题时放弃。具体白名单、几何判定及拒绝条件见 `history_evidence()`。不进行模糊补字，不从历史记录的模式或赛季反推当前对局。

输出的 `history_header_upper_half`、`history_support_below_header` 等是固定的证据代码；没有原始 OCR 字符串，也没有把 OCR 原始分数转成正确概率。白名单和位置阈值尚未通过真实标注集校准，无法保证已覆盖所有弹窗、遮挡、内嵌页面和相似布局；即使输出 `recognized`，也仅表示实验规则满足，不能用来宣称准确率或授权动作。

本次实机没有得到足够线索，单帧和运行时场景均保持未知；未为通过实测而放宽判定。实机共收到 10 帧，3 次异步识别结果均因较新画面到达而被拒绝。未保存原图，无法据此重建人工标注集。

## 6. 验证与后续缺口

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s src/jcc_device/tests -p test_state_pipeline.py -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s src/jcc_device/tests -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_game_state_v1.py -v
```

合成测试覆盖合法/未知封套、真实字节尺寸、来源时间、实验示意场景、未知商店与历史不适用、未核版本/模式、精确到期、完成时间屏障、不续期、陈旧/异流/异时钟/异代数结果、接收与交付间隙、显式切换、坏图像、断流、不可变快照，以及 Schema/语义反例。事件控制的异步测试证明晚任务在更新帧到达后被拒绝；这不是实际游戏中的识别正确率测试。

10B 原交付为 29 项新增、61 项设备层及 42 项契约回归。10E 的最新合成/回归数量、真实时钟慢识别、实机像素诊断及其限制见本地 `资料/开发/10E_识别连续性联调.md`；实机无校准路径仍保持未知，不把合成通过称为实机可用。

真实准备阶段的标注、独立验收场次和战斗字段识别样本均为 0。下一步需要真实且经独立复核的画面与标签、目标布局/ROI 标定、可靠的上下文/可见性证据及 TTL 校准，随后才能实现回合、血量、金币、等级和五格商店识别。版本敏感建议还需补丁/模式核验和资料映射。精确连续性仍可能因实际画面变化频繁而无输出，不能用未经校准的差异阈值或放行旧结果来提高发布率。
