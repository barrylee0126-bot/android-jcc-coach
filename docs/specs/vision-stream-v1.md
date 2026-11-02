# Vision stream v1：最新帧与识别底层接口

状态：可运行的取帧与原始识别增量；不是 M1 游戏局面识别验收。无真实游戏 ROI、场景分类器或金币／血量／等级解释器。本接口不依赖开发 08 的最终局面格式。

## 1. 组成与边界

| 入口 | 职责 |
| --- | --- |
| `jcc_device.stream.TabletStream` | 原有只读 scrcpy → 私有 FIFO → ffmpeg → 内存 JPEG；保留 `read_frame(timeout) -> bytes` |
| `jcc_device.latest.LatestFrameStream` | 独立接收线程持续读源，单槽替换尚未交付的帧；输出带来源的 `Frame` |
| `jcc_device.recognition.FrameProcessor` | 接受 `Callable[[Frame], T]`，记录处理起止与成功／失败数量；自身没有任务队列 |
| `jcc_device.recognition.LatestResultGate` | 在集成方明确推进当前输入后，拒绝旧帧、旧会话、重复和过期异步结果 |
| `jcc_device.recognition.recognize_regions` | 仅处理显式校准的 ROI；在内存裁切后调用现有 OCR，保留原始文字与分数 |
| `jcc_device.observe` | 原观察入口改用持续接收；保留旧汇总键，并增加来源、处理与取帧计数 |
| `jcc_device.stream_probe` | 可复现的假帧／只读平板实验，只输出时间与计数 |

接收线程与识别调用分离。识别慢时，新帧覆盖单槽中尚未被取走的帧，不排队等待识别。这里限制的是 **Python 应用内尚未消费的帧数为 1**：当前正在识别的帧、调用方自行持有的帧、底层解码缓冲仍可占用内存。不要把每帧都提交到无界线程池或异步任务队列，否则会重新引入积压。

本层持续排空现有 JPEG 源，但没有测量设备、scrcpy、ffmpeg、系统管道的内部缓存；不能保证拿到的是平板物理显示的最新画面。

## 2. 帧、时间与计数

`Frame` 包含 `source: FrameRef` 和 `jpeg: bytes`。JPEG 默认不参与 `repr`，`source.metadata()` 也不包含图像。图像不落到项目目录；底层 `capture.mkv` 是权限为 `0600` 的私有 FIFO，不是录屏文件。

`FrameRef` 字段：

| 字段 | 语义 |
| --- | --- |
| `stream_id` | 每次新建接收器生成的随机会话号，不是设备标识 |
| `sequence` | 本会话接收的完整、非空字节帧序号，从 1 递增 |
| `frame_id` | 属性，格式为 `stream_id:sequence`；`metadata()` 中包含此键 |
| `host_received_monotonic_ns` | 接收线程从 `source.read_frame()` 得到完整 JPEG 后立即读到的 Mac 单调时钟 |
| `host_received_utc` | 随后记录的 UTC 壁钟，供日志定位；不可用来计算延迟 |
| `host_received_time_source` | 固定为 `mac_python_complete_jpeg_read` |
| `device_capture_time` | 当前固定为 `None`；没有设备物理采集时间证据 |

单调时间仅适合相同 Mac、相同启动时钟域中的比较。接收时间晚于设备采集／显示，也可能晚于系统收到流数据的时间。不能把 `host_received_monotonic_ns` 改名为设备采集时间。不同采集会话用 `stream_id` 区分，重启 Mac 后的单调时钟不可直接比较。

`LatestFrameStream.stats()` 中：

- `frames_received`：接收的合法字节帧；`frames_delivered`：交给调用方的帧，不表示已经识别成功。
- `frames_replaced`：被更新帧覆盖的未读帧；`frames_discarded`：关闭、源失败或年龄限制丢弃的帧；`frames_expired` 是后者的年龄过期子集。
- `pending_frames`、`max_pending_frames`：当前／历史最大未读槽占用，取值 0 或 1。
- `source_timeouts`、`consumer_timeouts`：源读取等待与调用方等待超时；另列 `source_errors`、`close_errors` 与异常类型。
- `receiver_thread_alive`、`closed`：接收线程与调用方关闭状态。只有已结束的线程才能作为关闭完成证据。

任意已锁定的统计快照满足：

```text
frames_received = frames_delivered + frames_replaced + frames_discarded + pending_frames
```

`FrameProcessor.stats()` 单独记录 `frames_started`、`frames_processed`、`processing_errors` 和累计处理时间。`frames_processed` 只计成功返回的调用，不等于识别正确数。取走了首帧但只读元数据时，交付数会大于处理数。

`FrameResult.timing()` 包含来源、处理开始／完成的单调时间，以及 `processing_seconds`、`receive_to_finish_seconds`；后者只从 Mac 应用收帧算起。

## 3. 生命周期、失败和超时

`LatestFrameStream(source)` 接受**已经打开**的源；`start()`／上下文入口只启动接收线程。源协议要求 `read_frame(timeout)` 和 `close()` 都能有界返回。只有接收线程在启动后调用源，因此不会在另一个线程读管道时并发关闭它。

```python
from jcc_device.latest import LatestFrameStream
from jcc_device.recognition import FrameProcessor
from jcc_device.stream import TabletStream

processor = FrameProcessor(lambda frame: len(frame.jpeg))  # 替换为实际识别器
with TabletStream(capture_diagnostics=False) as source:
    with LatestFrameStream(source) as frames:
        frame = frames.read_latest(timeout=10, max_age_seconds=1)
        result = processor.process(frame)
        print(result.timing())  # 不含图像或 OCR 文字
```

- `read_latest(timeout=0)` 可轮询；没有新帧时抛 `TimeoutError`。取出的帧从单槽移除，不会被下一次读取重复交付。
- `max_age_seconds` 可选，限制帧在 Mac 应用中的年龄。过期帧丢弃后继续等待，直到有合格新帧或调用方超时。默认不设年龄阈值，集成方必须按字段／阶段需求指定。
- 源的 `TimeoutError` 可恢复，接收线程继续等；其他读取错误终止本次接收，清空槽并抛 `FrameSourceFailed`。错误只公开异常类型，避免源日志泄露设备标识。
- `close()` 立即阻止读取、清空槽并唤醒等待者，再等待接收线程退出和关闭源。重复调用不会重复关闭源。
- 若源不遵守超时或关闭约定，超过 `shutdown_timeout` 抛 `FrameShutdownTimeout`，并保留线程存活统计；Python 不能安全强杀这样的线程。测试用释放事件收回此类故障线程，不把它当作成功关闭。
- 关闭期间返回的帧只计接收与丢弃，不再交付。关闭或终止后不能重启相同接收器。
- 当前不自动重连。重连由上层新建源、接收器与结果门，并使旧局面失效；新的 `stream_id` 必须进入后续来源记录。

原 `TabletStream.read_frame`、`VisionOCR.recognize`、`VisionOCR.ocr` 和 `ReadOnlyTransport.exec_out("screencap -p")` 仍可使用。只有选择 `LatestFrameStream` 或新版 `observe` 的调用方获得持续排空；直接使用原始 `TabletStream` 的其他入口没有自动改变。

`TabletStream(capture_diagnostics=False)` 不保存或输出 scrcpy／ffmpeg 诊断内容；实验与 `observe` 使用此设置。原默认诊断选项保持兼容，启用时日志只在临时文件中，用后清理，错误消息可能含本地设备信息，公开输出前须由调用方处理。

## 4. 识别与迟到结果

`FrameProcessor` 不隐式发布局面。并发调用应由上层限制正在执行的任务数。`LatestResultGate(max_age_seconds=...)` 提供单输入结果发布规则：

```python
gate.advance(frame.source)             # 先把此帧选为当前输入，清掉旧结果
result = processor.process(frame)      # 可在有界执行器中运行
accepted = gate.publish(result)        # 只有当前帧、未过期的首个结果可发布
current = gate.get()                   # 读取时也会检查年龄
```

当更高序号的输入被 `advance()` 后，旧输入即使晚完成也被拒绝；接受判定不依赖完成顺序。不同会话、倒退时间、重复发布和不合理的处理时间同样拒绝。每帧若需要多方法融合，应由融合层组合成一个 `FrameResult`，本门不会合并同帧的多个结果。

**当前输入由集成方推进**：接收线程不会自动调用 `advance()`，本层也没有场景切换识别。上层在把某帧／场景视为当前状态时必须推进门；断流时必须 `close()`／使局面失效，不能只依赖年龄自然过期。若要求每个收到的帧立即使旧任务无效，应在集成层监听取帧并推进；对于耗时超过帧间隔的识别，这也可能导致没有结果可发布，需要明确采样／场景策略。新接收会话必须新建结果门。

`FrameResult` 与来源不可变；通用 `value` 由调用方提供，发布后应视为只读。字段的 `recognized/unknown/conflict/stale`、场景有效期、缺失解释、版本和多方法融合属于开发 08 契约及后续识别适配器，不在本层猜测。

## 5. ROI 与 Apple Vision

`PixelROI(name, image_size, box)` 采用左上原点的像素矩形 `(left, top, right, bottom)`，右／下边界不包含在内。尺寸必须与校准画面完全一致；换分辨率、方向或裁切需要重新提供配置，本层不暗中缩放。

`recognize_regions(frame, regions, ocr)` 接受非空、名称唯一的配置序列及实现 `recognize(bytes) -> list[dict]` 的 OCR。先验证所有 ROI，再以内存 PNG 调用现有 `VisionOCR`，把返回框转换为整帧像素坐标，并保留 ROI、来源和原始置信分数。不解析数字，不套用游戏字段，不把原始分数解释为正确概率。没有内置游戏 ROI。

`VisionOCR` 的持久子进程协议没有请求编号，因此同一实例的请求串行发送。超时预算覆盖等待前一请求、写入输入、读取完整换行响应；非阻塞管道避免“可读但响应未写完”导致无限 `readline()`。若已经开始的交换超时、EOF、协议或工作进程出错，终止该进程并丢弃协议状态；后续调用必须显式重新进入上下文／重开，不能消费旧进程的迟到行。只是在等待前一请求时超时的调用尚未发出字节，不影响正在进行的请求。

`close()` 会中断交换；身份检查与管道操作受同一锁保护，防止旧调用碰到重开后复用的管道描述符。`ocr(image, cls=False)` 的 PaddleOCR 2.x 形状适配保留。

交换超时后还需清理工作进程，方法返回耗时可能超过交换预算；终止等待最多 1 秒，必要时强制结束后再等最多 1 秒。重开与关闭都会等待同一份清理完成，不留下一条仍在退出的旧协议会话。

## 6. 复现与证据边界

在项目根目录运行，不需要新依赖：

```sh
PYTHONPATH=src .venv/bin/python -m unittest discover -s src/jcc_device/tests -v
PYTHONPATH=src .venv/bin/python -m jcc_device.stream_probe --source synthetic --sample-fps 4 --fast-frames 12 --slow-frames 6 --slow-seconds 0.8
```

第二条默认使用定速假字节源，没有设备、解码、OCR 或真实游戏含义。自动验证另以受事件控制的源和假 OCR 子进程覆盖替换、慢处理、恢复超时、断流、关闭失败、不合作源、迟到结果、ROI 校验及旧接口形状。

设备原本已亮屏且可提供流时，可按当次授权只读运行：

```sh
PYTHONPATH=src .venv/bin/python -m jcc_device.stream_probe --source tablet --sample-fps 4 --fast-frames 12 --slow-frames 6 --slow-seconds 0.8
tools/jcc-observe --frames 8 --sample-fps 4
```

实验沿用项目包装器选择默认设备，使用 `--no-control --no-window --no-power-on`，不触控、不唤醒、不启动游戏、不匹配；只输出来源、时间、数量或异常类型，不输出画面、OCR 文字和设备标识。失败仍返回 JSON 与非零退出码。首帧启动和预热后的持续读取分开记录；`frame_timings` 可逐项复算年龄与序号间隔。`close_seconds` 单列，接收器终止时的丢弃也进入计数。

任何合成源吞吐、合成文字 OCR 或基础取帧结果都不能代替真实场景／回合／金币／血量／等级准确率。真实 M1 仍需开发 08 的字段映射、标注集、实际游戏场景与 ROI 校准、未知／冲突／过期处理及独立测评。
