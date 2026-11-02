# 录像帧金币条件计算 v1

状态：独立离线原型。输入是私有录像中经解码的 `video_frame`，不是截图，也不是实时局面。复用现有数值 ROI、Apple Vision OCR、6/9 字形否决与 `jcc_economy_lab`；没有修改或绕过只接受截图的既有回放适配器。具体模式、版本、界面主体与数字含义仍未核实，本接口不生成实战建议、买卖动作或利息结论。

## 1. 一次离线分析

调用方在**项目外私有目录**准备一份 `video-economy-private-index-v1` JSON，包含录像路径和 SHA-256、无损 PNG 帧路径和 SHA-256、录像内的毫秒寻址目标、原始帧尺寸、金币数字候选区域，以及该区域的 Agent 目视候选。只接受 `source_kind=replay` 和 `capture_kind=video_frame`。索引本身也必须提供外部固定的 SHA-256；路径和私有索引不能放入公开仓库。

索引结构示意（路径、摘要、坐标、数字和时间均为占位符，不是真实样本）：

```json
{
  "schema_version": "video-economy-private-index-v1",
  "source_kind": "replay",
  "capture_kind": "video_frame",
  "video": {"path": "/ABSOLUTE/PRIVATE/VIDEO.mp4", "sha256": "VIDEO_SHA256"},
  "frame": {
    "path": "/ABSOLUTE/PRIVATE/FRAME.png",
    "sha256": "FRAME_SHA256",
    "seek_target_ms": 0,
    "seek_method": "ffmpeg_input_seek_first_frame",
    "image_size": [64, 48]
  },
  "roi": {"field": "gold_numeric_candidate", "box": [8, 8, 48, 32]},
  "agent_visual": {"observer": "agent_visual_unreviewed", "status": "candidate_clear", "candidate": 31}
}
```

摘要须替换为实际文件的 64 位小写十六进制 SHA-256；路径必须是项目外绝对路径，不能是符号链接文件，ROI 为原图中的左上、右下像素坐标，右下不包含。时间、截图、ROI、目视值、素材和来源不明均不得用默认值补齐。`agent_visual` 是 Agent 对同一原图及区域的未核验观察，不是用户标注、独立复核或真值；不接受把观察者声明为已核验的人。

该无损帧应与下述确定的解码方式一致：FFmpeg 在打开源录像前以 `-ss` 指定 `seek_target_ms`（毫秒换算为秒），选择 `0:v:0` 的首个输出帧并编码 PNG。适配器重新从**已核验摘要的同一录像文件描述符**解码该目标，比较整个原图的 RGB 像素。对不上就拒绝；不会通过改标成 screenshot 放行。此处时间只表示 FFmpeg 寻址目标；代码不声称它是实际帧 PTS、设备采集时刻或真人操作时刻。相同像素也可能在多个时间点出现，因此不能仅靠像素相同证明时间点唯一或录制的真实性。

在项目根目录运行（以下均为占位参数；标准输出包含私有摘要及时间，须保存在项目外）：

使用命令行的本机 Apple Vision 后端前，需在 macOS 安装 Swift 编译器、FFmpeg、Python 3.11 及 Pillow，并按仓库现有构建方式生成**本机** worker。源码 `src/jcc_device/vision_ocr.swift` 已随仓库提供，生成的 `tools/vision_ocr` 是构建产物，不在公开仓库中：

```sh
swiftc -O src/jcc_device/vision_ocr.swift -o tools/vision_ocr
```

这与 `tools/setup-macos` 内的编译步骤一致；完整初始化也可按仓库 README 操作。若 worker 不存在、不可执行或无法读取，命令行返回 `apple_vision_worker_unavailable`，需先检查或构建后重试。编译不是对实际识别质量或来源的认证。

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_video_economy \
  --index /ABSOLUTE/PRIVATE/INDEX.json \
  --index-sha256 INDEX_SHA256 \
  --hypothetical-spend 2
```

每次必须显式提供一个非负整数假设支出；它不是识别出的商店价格。CLI 使用本地 Apple Vision；导入接口也允许调用方显式注入本地 OCR，返回标明后端未经认证。没有设备、录屏、模型服务、后台循环或文件自动搜索。结果包含私有绑定摘要，不应发布、提交或用于追踪个人。

## 2. 拒绝与输出边界

先校验索引摘要、严格字段、录像及帧文件 SHA、源种类、目标时间、图像格式、尺寸和 ROI，再按同一录像文件重解码并对照全图像素。处理过程中复核帧与索引摘要、录像文件状态，以及本适配器、ROI、字形、经济模块和 FFmpeg 的源码／可执行文件摘要；发生变化则拒绝。使用内置 `VisionOCR` 时，另记录 Swift 源码与本机 worker 摘要，命令行在 worker 启动前、分析内和退出后对照依赖摘要；缺 worker 不继续分析。传入假 OCR 或其他调用方自带后端时，不要求或声称校验 Apple Vision worker，`worker_fingerprint_checked=false`；该后端自身的实现与依赖未经本接口认证。源码与可执行文件的摘要并不能证明二者存在编译关系；文件状态和前后摘要检查也不构成对抗恶意篡改或源码载入过程的认证。

针对已绑定帧，两个 OCR 倍率使用相同 ROI 和同一后端；任一缺失、失败、冲突或数值无效，均不计算。含 6/9 的候选还须通过既有像素字形否决；该检查不证明数字正确或区域完整。Agent 目视候选缺失、不可见或与 OCR 不同，也不计算。两个倍率一致及 Agent 与机器一致，均不是准确率或独立人工真值。

结果分别保留 `agent_visual`、`machine_ocr`、`human_ground_truth=null` 与 `independently_reviewed=false`；两个 OCR 请求裁图和 `recognize()` 返回值以摘要绑定，并在私有结果中保留返回值的规范化 JSON，供后续重算摘要。下游数字解析使用与记录同一个反序列化快照，不再读取后端提供的可变对象；非有限数值和无法规范化的返回值不用于计算。内置 VisionOCR 的这个返回值是其解析出的列表；没有保存 worker 的原始线路字节或外层协议包装。假 OCR 或其他后端同样只保存其 `recognize()` 的返回值。结果也保留索引／录像／帧摘要和寻址目标，均应按私有资料处理。`scene/mode/game_version` 保持未知，己方主体未核验。机器候选只支持“如果该数字代表金币且等于 G”的离线条件算术；既不证明画面可操作，也不证明该数字属于自己。

经济调用固定为 `analyze_purchases(..., rule=None)`。若金额和单笔假设均有效且未超额，返回条件余额及 `cash_only`；若支出超过候选余额，返回 `insufficient_candidate_gold`，余额为 `null`。任何情况下利息字段均为 `null`，且 `real_rules_verified=false`、`production_eligible=false`、`execution_eligible=false`、`strategy_recommendation=null`、`action=null`。没有已核实的国服模式、版本、计息时点、强化或真实商店价格，不能将条件余额解释为值得购买或可执行动作。

本接口每次重新读取并计算，没有跨帧缓存或真实对局时效保证。输出对调用方可见的 SHA-256 是关联私有材料的标识，输出亦须私有保存。真实录像经单帧管线处理，不等于机器候选准确率、用户阅读体验或上分能力通过验证。

## 3. 合成验证

```sh
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s src/jcc_video_economy/tests -v
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s src/jcc_economy_lab/tests -q
```

合成测试使用自行生成的短视频、无损帧和假 OCR，不包含游戏画面，不要求 Apple Vision worker 或 Swift 源码构建产物；需有可用 FFmpeg、Python 3.11 及 Pillow。覆盖真实 FFmpeg 解码与全帧像素匹配、索引／录像／帧摘要、错误寻址、来源类型、尺寸与 ROI、未知目视、机器分歧或失败、6/9 否决、超额支出，以及处理过程中的源文件或依赖变化。OCR 结果只是合成返回；测试通过不代表真实识别成绩。
