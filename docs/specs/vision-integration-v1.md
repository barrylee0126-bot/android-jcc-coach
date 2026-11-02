# Vision integration v1：连续性与局面合并的最小联调

开发10E，2026-11-02。实现仅涉及 [`latest.py`](../../src/jcc_device/latest.py)、[`state_pipeline.py`](../../src/jcc_device/state_pipeline.py) 及 [集成测试](../../src/jcc_device/tests/test_state_integration.py)；不改动10C `stability.py`、共享 Schema 或契约验证器。10C 文档中“尚未接入10B”描述的是其原始交付，本文件记录后续的可选接入。

## 1. 前提与使用

默认 CLI 和 `StatePipeline(continuity_enabled=False)` 保留10B的全输入屏障。新增的连续性路径可关闭；它只覆盖现有场景识别及由该场景推导的“不适用”，没有新增金币、血量、等级、商店内容、模式、版本或策略能力。

```python
stream = LatestFrameStream(already_open_raw_source)
pipeline = StatePipeline(
    stream_id=stream.stream_id, sample_kind="live",
    continuity_enabled=True,
    calibration=reviewed_calibration_or_none,
    context_provider=bounded_per_frame_verifier_or_none,
)
stream.bind_observer(pipeline)  # 只在启动前绑定一次，不叠加旧 InvalidatingFrameSource
with stream:
    frame = stream.read_latest(timeout=1)
    context = pipeline.advance(frame)  # 只能选择已在权威接收路径检查的当前帧
    result = processor.process(frame)  # 实际运行时由原有单工作执行器执行
    accepted = pipeline.publish(context, result)
    current = pipeline.snapshot()     # 每次消费都重新检查；复制件只是当时快照
```

`calibration=None` 或没有该帧适用的 `ContextEvidence` 时输出未知，即使单帧 OCR 给出正向实验结果也不能合并。提供者必须返回与当前真实 `FrameRef`、时钟、方向和校准相符的证据；不能用固定真实场景、上次 OCR 或相同像素代替本帧的上下文/可见性确认。提供者异常不输出异常原文，按未知处理。

当前 `SceneRecognizer` 的 OCR 输入是整幅图像，包括可能出现的阻断标签，因此 `relevant_regions` 并集必须覆盖整个已解码整帧。只标定标题框会得到 `recognizer_coverage_unproven`。`context_regions` 仍须有明确配置，覆盖是否足够及验证方法是否可靠由真实校准负责，代码不会替调用者认证。尚无这样的实机校准/验证器。

## 2. 接收线性化及关闭

1. 底层源返回完整非空字节后立即记录真实主机接收时刻；不持状态锁等待下一张图。
2. 有序接收者取得 `pipeline.receive_lock`，分配本次流的真实 `FrameRef` 和递增序号。这是应用权威帧的分配边界。序号不因消费者丢帧而重编。
3. 在同一可重入锁内调用 `observe_frame`：逐帧上下文检查、10C精确 RGB 比较、实际尺寸读取、检查预算判断和局面更新。`publish/snapshot` 也持同一锁，不能越过尚未检查的新权威帧。
4. 检查及状态提交完成后才进入可替换的最新槽。识别任务可以跳帧，检查不能跳帧。消费者拿到旧槽帧时，`advance` 会等待在途检查，再拒绝已经落后的交付。

接收缺口、乱序/重复、时间非法、解码失败、上下文/可见性未知、几何不匹配、检查超时或像素变化均打断旧段。A→B→A 中的 B 已在替换槽之前被检查，最早 A 不会复活。解码失败保留旧锚点的真实元数据并撤销值，不为坏图像生成猜测尺寸；后续有效帧可开始新段。

默认 `max_check_seconds=0.25`、`max_frame_age_seconds=1.0` 和 `scene_ttl_ms=2000` 是未校准工程参数。检查预算包含当前提供者、RGB 连续性检查及实际尺寸解码，超过预算或检查结束时帧已超龄则该帧未知。即使10C在某个预算超时后仍有内部同像素代号，10E也会封闭旧上下文；后续只能建立新的内部代数，旧任务不能恢复。

提供者必须有界。Python不能强杀卡住的同步提供者；锁会挡住相关发布及读取，`LatestFrameStream.close()` 先唤醒消费者，再在总关闭预算内等待所有者锁和接收线程。预算届满抛 `FrameShutdownTimeout`、保留 `receiver_thread_alive=true`，不声称已关闭。回调最终返回后，接收者在释放所有者锁前处理停止并断连，不把结果放回已关闭槽。提供者返回前无法获得新有效快照。回调异常或断连回调异常均只记录类型，源仍会关闭。

尚未由这个有序接收者分配身份的源内、scrcpy、ffmpeg、操作系统管道字节，不在应用证明内。接收时刻可能早于等待状态锁的时间；这些等待不被伪造成较新的接收时间。没有设备采集时钟/积压上界，不证明物理画面最新。

## 3. 同段合并与水位

逐帧检查相同且证据成立时，局面锚帧推进，场景组保留原观察封套，避免每张新帧无条件抬高该组字段水位。未实现的当前模式、补丁仍引用最新锚帧保持未知。其他情况分配新的内部 `context_epoch` 并建立新帧未知屏障；几何、校准与元数据不在同一代数中重写。

10C 的 `ContextEvidence.context_epoch` 是外部验证器的身份条件，10E 的局面 `context_epoch` 是其唯一状态所有者的失效代数；二者不靠数字相同互相证明。内部代数与完整连续段绑定，外部代数/身份/验证方法变化、预算失败或未知都会开启新的内部代数。结果须同时满足所属段的真实票据及内部代数，不能重建或改写旧 `FrameContext` 后继续提交。

合并顺序：

1. 重新检查输入健康、流/时钟/内部代数，以及10C `validate_result`。证明末帧必须等于当前锚帧，原始来源与处理起止时间须一致。
2. 检查每个受影响字段的最后来源序号与同帧完成时间。较新未知同样是屏障；不能因连续性证明而回退。每个来源只接纳一个结果，已接纳更高来源之后的旧结果拒绝。
3. 不自动解除现有冲突；单结果场景 API 没有多方法复核权限。仍有效的冲突保留；候选到期撤为未知。非法区域/字段仍由共享契约拒绝。
4. 用**原来源帧**、原接收/完成时间和 `source_received + scene_ttl_ms` 组装，不改写成证明末帧，也不因新帧或重复识别续期。精确到期条件为 `now >= deadline`。
5. `validate_state(candidate, previous=accepted)` 通过后由同一状态所有者原子发布。保留当前锚帧及字段/候选所引用的原始帧表；尚未引用的待识别源只能凭同段证明恢复其原几何和时间，不生成替代序号。

`snapshot()` 重新检查原 TTL 和接收活性。场景到期保留原 `stale` 诊断来源，并撤销其“不适用”推断；断流/停止立即封闭任务。若主机时钟倒退，连续性路径终止，时钟数值后来恢复也不会复活旧流。

## 4. 命令、测试与实机边界

```sh
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source synthetic --experimental-history --continuity synthetic --synthetic-delay-seconds 1.45
PYTHONPATH=src .venv/bin/python -m jcc_device.state_pipeline --source synthetic --experimental-history --continuity unconfigured
PYTHONPATH=src .venv/bin/python -m unittest discover -s src/jcc_device/tests -p test_state_integration.py -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s src/jcc_device/tests -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_game_state_v1.py -v
PYTHONPATH=src .venv/bin/python -m jcc_device.tests.test_state_integration --tablet-smoke
```

最后一条必须显式运行才连接默认平板，单元测试发现不会连接设备。它沿用项目默认设备包装器和只读源，不唤醒、不触控、不启动匹配、不保留原始子进程日志。诊断将全部相邻完整 RGB 图像逐像素比较并计数，同时**每帧都返回 `None` 上下文证据**，绝不把这些完整图像诊断冒充已校准的语义 ROI。图像只在内存中短期保留上一帧；不保存图像、哈希、账号、设备标识或 OCR 原文。

合成测试覆盖虚拟及真实时钟4 fps/慢识别进入完整契约、局部单像素变化、A→B→A、丢帧/乱序、上下文未知、无校准、TTL、检查预算、并发读取/发布、关闭超时、源故障、水位、冲突和契约拒绝。实机观测与计时见本地 `资料/开发/10E_识别连续性联调.md`。

合成通过只证明该工程链路在明确前提下可合并。真实场景/遮挡验证、ROI/TTL校准、人工标注、独立验收、实际补丁/模式核验和战斗字段识别均仍缺失。整帧精确策略也可能因动画、压缩或细小变化持续撤销；没有引入未经验证的差异阈值，不给实战准确率或上分能力结论。
