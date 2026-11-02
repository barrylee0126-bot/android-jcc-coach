# Vision stability v1：接收帧连续性与异步识别调度

开发10C，2026-11-02。实现为 [`stability.py`](../../src/jcc_device/stability.py)，测试为 [`test_stability.py`](../../src/jcc_device/tests/test_stability.py)。与 [vision-stream-v1](vision-stream-v1.md) 和 [game-state-v1](game-state-v1.md) 配合使用；本增量没有修改原接收器、`LatestResultGate`、局面契约或10B合并器。

## 1. 解决的问题与证明范围

逐帧推进 `LatestResultGate` 时，4 fps 输入每250毫秒到达一帧；若识别耗时约1.45秒，结果完成前输入已多次推进，静止区域也无法形成可用输出。本接口允许原始来源帧的结果在**原始截止时间之前**，凭连续接收帧的精确区域比较，交给合并层继续审核。

证明严格限定为：调用方明确校准的相关区域和上下文区域，在来源帧至最新检查帧这段**实际接收到、按连续序号逐一检查的 RGB 图像样本**中相同；调用方同时为每帧提供适用的上下文与无遮挡证据。

它不能证明设备端未采样的中间画面、设备到Mac的内部缓冲、区域外像素、隐藏游戏状态或语义没有变化。相同文字/图标也不能证明商店购买仍然有效。`available` 仅表示原始识别结果有可核对的连续性与时间依据，不表示识别正确、已达到 M1 或可安全用于购买/实战。本增量没有真实游戏坐标、场景/遮挡检测器或真实样本校准。

## 2. 配置与每帧上下文

`RegionCalibration` 是不可变配置，包含：

| 字段 | 约束 |
| --- | --- |
| `calibration_id`, `calibration_ref` | 非空身份及可追溯校准引用；调用方保证引用对应真实覆盖检查，模块不代为认证。 |
| `image_size` | 已校准整幅解码图像的精确 `(width, height)`；没有自动缩放。 |
| `relevant_regions` | 非空 `PixelROI` 元组，完整覆盖这组识别实际使用的像素；原点和右下排除规则沿用 `vision-stream-v1`。 |
| `context_regions` | 非空 `PixelROI` 元组，覆盖判定相关场景、阶段、布局、视区和遮挡所需像素。静态装饰边框不能替代这些内容。 |
| `applied_rotation_degrees` | 已应用到图像的旋转，0/90/180/270；不能靠尺寸猜测180度旋转。 |

所有区域名称跨两个集合唯一，区域须落在同一精确校准尺寸内。允许区域重叠，重复覆盖部分会重复复制/比较。构造时拒绝空集合、坏坐标或无效配置；尚未校准时传 `calibration=None`，`observe` 返回 `unknown/regions_unconfigured`，不能取得识别票据。

`ContextEvidence` 必须对**每个接收帧**显式提供：

- `source`：必须等于该帧的完整 `FrameRef`，不能直接复用上一帧的证据对象。
- `host_clock_id`：与证明器同域；`context_epoch`：非负、不可倒退。
- `context_key`：已证明的相关场景/阶段/模式/布局/视区等身份。缺少其中会影响本组识别的上下文时，用 `None`。
- `calibration_id`、`verification_ref`：适用的区域校准及上下文/可见性验证方法引用。更换验证方法也会打断连续性。
- `orientation`、`applied_rotation_degrees`：必须与解码尺寸及校准一致。
- `unobscured`：只有经当前帧证据确认无遮挡时为 `True`；遮挡为 `False`，无法确认用 `None`。两者都拒绝发布。

这些字段是调用方的前提证据；不可用旧 OCR、固定默认场景或模型自评置信度填成“已证明”。模块会验证身份、自洽性和连续性，但不能自行证明调用方所选区域足够，或上下文判断本身正确。当前没有通用的实机场景/遮挡验证器；不能提供上述证据的运行路径必须保持未知。

## 3. 连续性接口与失效规则

```python
tracker = ExactPixelContinuity(
    calibrated_coverage_or_none,
    host_clock_id=the_current_host_clock_id,
    max_result_age_seconds=the_result_policy_seconds,
    max_frame_age_seconds=the_receive_liveness_policy_seconds,
)
check = tracker.observe(frame, context_evidence_or_none)
ticket = tracker.ticket()  # 只针对最近一张完整检查通过的帧；不能为旧帧补开
# 识别可在调用方自己的有界执行器中进行。
result = processor.process(frame)
decision = tracker.validate_result(ticket, result)
```

示意代码只说明调用顺序；只有 `ticket is not None` 且与提交的 `frame.source` 相同才可执行识别。调用方也可采用下节内置调度器。

`FrameCheck.status="ready"` 表示可以识别该帧，尚无游戏字段结论；`unknown` 表示不可识别/继承。`RecognitionTicket` 只在当前证明器内使用，不能序列化后重建成可继续使用的票据。`proof_for(ticket)` 检查票据连续性和有效期；`validate_result(ticket, result)` 再核对完整来源及处理起止时间。

| 事件 | 行为 |
| --- | --- |
| 配置区域 RGB 字节完全相同、上下文相同 | 保持连续性代号 `generation`。比较的是解码后区域字节，不是JPEG/PNG文件字节，不使用像素差阈值或末帧哈希。 |
| 相关区域或上下文区域出现任何像素差异 | 立即更换代号，旧票据永久失效；当前帧可作为新段起点，需重新识别。 |
| `context_epoch/key/verification_ref` 变化 | 即使像素相同也更换代号；在新上下文已明确且无遮挡的前提下可开新段。代数倒退返回未知。 |
| 未配置、遮挡/可见性未知、上下文未知/错帧、校准/尺寸/方向/旋转不匹配、坏图像 | 清除连续性，返回未知，旧票据不能恢复。 |
| 接收序号缺口 | 缺口帧本身未知；随后连续、有效的一帧可开新段，不能把缺口纳入旧证明。 |
| 重复/乱序帧 | 清除证明并拒绝该帧，保留最高序号，不能静默忽略后继续声称原段连续。 |
| 接收时间倒退或在未来 | 清除证明并拒绝，时间最高水位不倒退。 |
| 最新检查帧达到 `max_frame_age_seconds` | 读取/提交时立即未知；新帧恢复时也不能恢复旧票据，即使期间没有人读取证明。 |
| 会话或主机时钟域变化、单调时钟倒退、`disconnect()` | 终止当前证明器；重连/新会话须新建对象。 |
| `now >= source_received + max_result_age` | 原票据及结果到期，仍有相同新帧也不能续期；可对新来源帧执行新的识别。 |

`A→B→A` 中 B 被每帧检查发现后已更换代号；末帧回到 A 不能让最早 A 的票据复活。新段 `ready` 不等于旧结果再次可用。颜色只接受静态 RGB JPEG/PNG；alpha、调色板、动画图像和未应用的 EXIF 方向等保守返回未知，避免转换丢弃被比较的信息。

`ContinuityProof` 不包含图像，包含原始 `source`、`verified_through`、`checked_frame_count`、代号、时钟域、上下文、完整校准配置、当前检查时刻和原截止时间。计数为 `verified_through.sequence - source.sequence + 1`；只有完整、连续、全部通过的接收段才会产生证明。它是本进程检查的审计摘要，不是可脱离本证明器重新播放/独立认证的证书。

## 4. 异步调度接口与资源边界

```python
scheduler = StableRecognitionScheduler(tracker, processor).start()
try:
    # 每个接收帧同步调用；这一步解码/比较区域，但不执行 OCR。
    check = scheduler.observe(frame, context_evidence_or_none)
    # 显示、局面合并和建议使用前均重新读取，不缓存 available 状态。
    read = scheduler.current()
    if read.status == "available":
        original_result = read.result
        continuity_proof = read.proof
finally:
    scheduler.close()
```

`processor` 接受现有 `FrameProcessor`，或实现 `process(Frame) -> FrameResult` 的等价对象。每个调度器只有一个识别线程、一个正在执行的任务和一个可替换待识别槽。检查通过的新帧只替换待识别帧；完成后从最新待识别帧开始下一次识别。过期/失效待识别任务在运行前丢弃；已执行的旧任务不强杀，返回后重新校验，失败不能发布。

解码、逐区域比较和证明校验共享锁；检查正在进行时，结果提交/读取会等待检查完成。调度器自身的接收、结果替换、读取、断连也串行协调。识别异常只记录异常类型，清除可用结果，随后可处理新的待识别帧。断连必须在发现时立即调用 `scheduler.disconnect()`，清掉可用值及待处理帧；`close(timeout_seconds=...)` 再有界等待执行线程退出，超时抛 `TimeoutError` 并保留 `worker_alive=true`，不冒充关闭成功。重连不能重启相同对象。

`current()` 返回 `RecognitionRead(status, reason, result, proof)`；未知时后两项为空。之前返回的只读对象是**当时**的判断，模块不能撤回调用方复制的值；使用方必须在每次使用时重新检查，显示层还须按截止时间/断连/新帧失效事件撤销提示。

内存上界是此模块持有一个段的区域基准字节、一个执行帧、一个待识别帧、一个已接受的无图像结果，以及检查时的临时解码/裁剪图像。区域基准为 `3 * sum(ROI面积)` 字节，临时新区域样本再占同量级；整图解码及Pillow缓存另计。泛型结果 `value` 不应包含额外图像；调用方/识别器/底层流自行保留的对象不受本调度器限制。

## 5. 接入10B的最小约定

1. 在**所有帧的接收路径**分配现有权威 `FrameRef` 后，且在任何有损单槽替换之前，同步调用 `observe`；不能只观察 `LatestFrameStream.read_latest()` 的交付帧。当前 `LatestFrameStream` 没有这个钩子，本交付未修改它；后续由拥有者增加接收回调或建立统一接收所有者，并同步协调帧到达、检查和状态发布。不得给交付子集重新连续编号来掩盖遗漏。
2. 每帧提供上述经校准的上下文/可见性证据。每个代号变化或未知，唯一合并者先撤销这组字段；场景/布局等变化按 `game-state-v1` 增加上下文代数。即使没有新识别结果也必须处理失效，不能等 OCR 返回。
3. 只有 `validate_result(...).status="proven"` / `current().status="available"` 才进入字段解释与合并审核。保留原 `source_frame_id`、观察/处理时刻和原始截止时间，锚帧仍更新为最新接收帧；所需原帧元数据留在 `frames` 表中。纳秒转毫秒使用 `/ 1_000_000`，不能改写来源为证明末帧。
4. 证明只适用于配置中完整覆盖的字段。字段自身有效期只能小于等于票据原期限，仍须做字段屏障、冲突、上下文及 `validate_state(candidate, previous=accepted)` 校验。已发布的更高来源水位不能被证明绕过；如果旧代码已把每张新帧都推成字段未知水位，应等待相应新帧的识别。未来适配时，只有在完整连续性检查通过的路径，才可保留尚未到期的旧来源字段而不无条件推进其字段屏障。
5. 若10B已有有界执行器，可只接入 `ExactPixelContinuity.observe/ticket/validate_result/disconnect`，无需再开一套调度器。必须按上述同步关系协调合并；相同新帧的像素连续性不能证明外部异步上下文任务已完成。

接收源已经读完但还没进入本模块、其他线程提前把新帧作为当前锚帧发布、或输入只来自丢帧消费者时，本模块不能补足缺失证据。集成方必须明确接收线性化边界；不能把本地锁解释为设备/源内部也没有新画面。本增量没有接入10B，因此当前产品行为尚未因它改变。

## 6. 开销测量与复现

每个实际接收帧都必须完成：序号/会话/时钟/几何检查、当前帧上下文与遮挡验证、RGB解码、全部相关及上下文区域提取与精确比较。可以只对部分帧运行慢 OCR，不能只对那些帧运行连续性检查。逐帧检查若跟不上输入，不得把省略的帧算成已验证；应未知并记录缺口/积压，由集成方改进链路。

完整图像解码随整幅像素数增长，区域提取和相同帧精确比较随配置区域面积总和增长；重叠区域重复计算。下列合成计时不构成任何内容、分辨率或负载下的硬上限。

`FrameCheck.check_elapsed_ns` 与 `stats().check_ns_total/check_ns_max` 用 `perf_counter_ns()` 记录 `observe` 的锁等待、检查、解码、裁剪和比较时间；`region_bytes_extracted`、`frames_decoded/ready/unknown` 同列。调度器 `stats()` 另记录作业、接受/拒绝、待识别替换、活动/待识别上界及异常类型。外部上下文验证若在调用 `observe` 前执行，耗时必须另外测量；这些计时也不包括设备传输、外部OCR、局面合并或用户可见提示。

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s src/jcc_device/tests -p test_stability.py -v
PYTHONPATH=src .venv/bin/python -m jcc_device.tests.test_stability --benchmark
PYTHONPATH=src .venv/bin/python -m jcc_device.tests.test_stability --stream-demo
```

单测用可控虚拟时钟、RGB合成图和事件控制线程，覆盖4 fps/1.45秒、变化/上下文/A→B→A、缺口/乱序、有效期边界、关闭、慢检查与异常。`--benchmark` 默认两种合成条纹JPEG尺寸各120帧，不等待、不做OCR、无固定速度及格线。`--stream-demo` 用真实单调时间以目标4 fps提交合成图，假识别器实际等待约1.45秒，并检查原结果2秒到期后的未知空窗；不是 Apple Vision 或实机成绩。2秒/0.5秒等策略值仅用于合成反例，不是经校准的生产配置。

测试与计时的本次结果、实机缺口见本地 `资料/开发/10C_识别连续性与调度.md`。
