# RapidOCR candidate v1：独立固定 ROI 转写原型

开发18C，2026-11-02；18D补充原始后端结果校验与历史输入计数修复。实现位于 [`src/jcc_rapid_ocr_candidate`](../../src/jcc_rapid_ocr_candidate/)。默认不安装、不导入 RapidOCR 或其他图像/推理依赖，不启动任何进程。

**所有文字结果均为 `transcription_only`，不具几何证据，非真人标注，无实战授权。成功也只是 `development_candidate`；本模块不产生金币、英雄等游戏确定值。** 未提供 `VisionOCR.recognize()` 形状的适配器，不生成文字框、game-state、策略建议或买卖动作，也未接入任何现有模块。

## 1. 固定输入约定

调用方显式提供 `FixedROI(roi_id, width, height)` 和**已经裁好**的 PNG 字节。`roi_id` 是调用方对固定区域的命名，宽高必须与实际解码尺寸完全一致。本原型不从整帧寻找、裁切、旋转、缩放或校准 ROI，不读取设备、视频、摄像头或远程图片。

这份输入约定只约束当前裁切的身份声明与尺寸，不能证明其在整帧中的位置、字段身份、原帧来源、遮挡情况或新鲜度；相同尺寸与摘要也不证明来自合法场景。没有文本框意味着无法判断文字贴边、跨界、缺字或多行遗漏。**裁切的四边不得冒充检测到的文字四边。**

- 单个 PNG 最多 4 MiB；宽高各 1–4096，约定面积最多 200 万像素。
- 仅支持完整可解码、CRC 校验通过的单帧 PNG，模式为 RGB/RGBA/L，EXIF Orientation 为空或 1。
- 字节摘要由本模块计算；子进程再次验证摘要、尺寸、编码和请求序号。原始字节直接送给官方库，不做二值化、纠字、数字解析或词典回填。RapidOCR 自己的默认预处理和识别归一化仍存在。
- 仅有一个非空转写且分数合法、达到显式工程阈值时才返回开发候选。多转写不拼接、不挑最高分；空文字不代表空槽。

## 2. 固定上游、许可与本地资产

| 组件 | 固定来源与约束 |
| --- | --- |
| 官方库 | [RapidAI/RapidOCR v3.10.0](https://github.com/RapidAI/RapidOCR/releases/tag/v3.10.0)，发行提交 `bd941dae09da793e76c6ba15c7aaf40f8424232a`；执行官方 PyPI `rapidocr==3.10.0` wheel，SHA-256 `2fc34e26cd0f48514804a94f8832283039c7334076bff60768b1fed9ad068050`。 |
| 调用接口 | [`RapidOCR(params=...)`](https://github.com/RapidAI/RapidOCR/blob/bd941dae09da793e76c6ba15c7aaf40f8424232a/python/rapidocr/main.py)，固定 `use_det=False, use_cls=False, use_rec=True`；使用该固定版本的 `_load_rec_model()` 提前加载惰性识别模块。 |
| 输出形状 | 官方 [`TextRecOutput`](https://github.com/RapidAI/RapidOCR/blob/bd941dae09da793e76c6ba15c7aaf40f8424232a/python/rapidocr/ch_ppocr_rec/typings.py) 只有识别字符串、分数等字段，不提供检测框。`word_results` 也不作为本原型的几何证据。 |
| 推理后端 | `onnxruntime==1.23.2`，只接受 `CPUExecutionProvider`；检测、分类和其他执行提供者均禁用。 |
| 唯一接受的权重 | 调用方显式指定的现有 `PP-OCRv6_rec_small.onnx` 内容，21,234,383 字节，SHA-256 `6f327246b50388f3c176ae304bd95767ea6dc0c9ae92153ef8cbe210b3c14884`。文件名本身不能替代内容校验。 |
| 依赖锁 | 包内 [`requirements-18a.lock`](../../src/jcc_rapid_ocr_candidate/requirements-18a.lock)，复用18A核查过的 24 个官方二进制 wheel，包括 NumPy 2.3.5、OpenCV headless 5.0.0.93、Pillow 13.0.0、OmegaConf 2.4.0。哈希针对已核查的 macOS ARM64 发行件。 |

RapidOCR 源码采用 [Apache-2.0](https://github.com/RapidAI/RapidOCR/blob/bd941dae09da793e76c6ba15c7aaf40f8424232a/LICENSE)。该权重的来源和单独许可见官方 [`MODEL_LICENSES.md`](https://github.com/RapidAI/RapidOCR/blob/bd941dae09da793e76c6ba15c7aaf40f8424232a/python/MODEL_LICENSES.md)：源于 PaddleOCR，归属百度及相应上游权利人，RapidOCR 转换件按 Apache-2.0 条款再分发。本原型未复制上游源码、wheel、权重或游戏素材到仓库；源码许可不自动覆盖任意第三方权重和游戏图片。离线依赖的许可文件随已核查的 wheel 保留，运行时一并核对文件内容。

此轮没有训练模型、自制识别器或修改上游算法，也不声称支持新的库版本、权重、系统或处理器平台。缺失模型、字典、wheel、已安装文件或版本/哈希不一致均明确失败；即使官方 wheel 自带权重，也不允许省略调用方路径后自动选取。

## 3. 离线执行与启动过程

`RuntimeConfig` 必须接收三个现有绝对路径：已审计的 Python 3.11 虚拟环境、18A 的 24 个 wheel 所在目录、识别模型。没有安装器、下载器命令、动态插件、任意配置文件或任意导入路径参数。

环境参数不授权执行任意程序：启动前将其 `bin/python` 解析到18A已验证的 Homebrew Python 3.11.16 固定路径，同时核对解释器和其 `Python` 动态库摘要；随后直接执行已核对的解析路径。指向其他解释器或内容变化会返回 `unverified_python_runtime`。这项原型因此也不自动适应 Homebrew 升级或另一台主机，须先重新审查指纹。

真实推理由常驻子进程执行，启动命令固定为 macOS `/usr/bin/sandbox-exec`，策略包含 `(deny network*)` 和 `(deny file-write*)`。环境变量只传递固定的 `PATH` 与 `LANG`；Python 使用 `-I -S -B`，不执行 `site`、`.pth`、用户启动钩子或调用方 `PYTHONPATH`。缓存目录被指向不存在的 `/dev/null` 子路径，既不写入也不读取环境中的缓存字节码。

子进程先通过 macOS 系统库的 `sandbox_check` 只读检查出站、入站和 bind 均被禁止，**不通过尝试连接探测禁网**。系统策略或检查接口不可用时拒绝启动，不改用联网执行。当前实现仅支持本轮核查的 macOS ARM64 / Python 3.11 环境；Linux、Windows 等平台返回 `network_isolation_unavailable`。

随后核对模型长度/摘要、24 份完整 wheel 摘要和对应已安装文件的逐字节内容，再允许导入。pip 改写的安装清单不作为代码；可能被重建的 `.pyc` 不批准也不读取。导入守卫拒绝未在固定 wheel 中验证过的非标准库模块。调用方提供的 Python 解释器、Python 标准库和 macOS 系统库是这里的信任基础，不由 wheel 校验认证。

wheel 文件审计将安装目录和目标文件分别解析后再判断包含关系，支持 macOS `/var` 等上级目录的符号链接别名。传入的安装目录本身、已安装文件或 wheel 文件若是符号链接仍拒绝；子目录指向安装目录以外的目标，以及 wheel 内的绝对路径和 `..` 路径也仍被拒绝。

构造官方 engine 时显式设置本地识别模型、关闭检测/分类/文字框及其他设备提供者，并把官方 `DownloadFile.run` 替换为固定的 `download_forbidden` 失败入口；OS 策略仍是网络限制的执行边界。模型加载完成后再次核对摘要和实际 provider。`start()` 的 `ready` 表示模型已加载，**尚不等于做过推理预热**。

每次推理先检查实际返回对象属于已验证依赖中的精确 `TextRecOutput` 类型，不接受仅有相同类名的对象。`txts` 与 `scores` 必须原本就是长度相同的 `list` 或 `tuple`；各文本必须是有界字符串，各分数必须在转换之前属于有限且位于 `[0, 1]` 的 `numbers.Real`，并排除 Python 布尔值。固定 NumPy 的实数标量可通过，NumPy 布尔值、字符串分数、复数及数组均被拒绝。只有这些检查通过，才转换为 JSON 所需的列表与浮点数；字符串、映射和迭代器不会被拆开洗成合法文本容器。意外框不进入管道。预热也走同一检查，畸形返回不会得到 `warmed`。

这些校验是固定本地运行路径中的防错约束。项目自身的 worker、契约、依赖锁及此处规格也属于受信本地代码；它们未由 wheel 校验认证。操作者若能改写本地代码、锁、检查器、已审计解释器或正在运行的进程，可改变校验行为。精确类检查、文件摘要和 OS 禁网不构成对整个主机或同权限操作者的认证。

`warmup()` 用标准库在内存生成纯白 RGB PNG，调用同一个官方识别器；不读取真实画面，不把空白的机器输出当标注。预热成功后才接纳 `transcribe()`。关闭或失败时结束子进程，不保留后台模型、输出文件或缓存，不自动重启、重试、补装或切换 OCR。

## 4. Python 接口与结果

下面只使用内存生成的虚构图片；路径占位符须替换为调用方已有的受控资产。

```python
from pathlib import Path
from jcc_rapid_ocr_candidate import (
    RuntimeConfig, RapidOCRCandidate, FixedROI, CropRequest, reconcile,
)
from jcc_rapid_ocr_candidate.contract import blank_png

config = RuntimeConfig(
    environment=Path("/absolute/audited-venv"),
    wheelhouse=Path("/absolute/reviewed-wheels"),
    rec_model=Path("/absolute/local-models/PP-OCRv6_rec_small.onnx"),
    timeout_seconds=30.0,
    minimum_raw_score=0.5,
)
crop = CropRequest(FixedROI("synthetic.single-line", 96, 48), blank_png())
with RapidOCRCandidate(config) as backend:  # start; record backend.startup
    warmup_metadata = backend.warmup()
    a = backend.transcribe(crop)
    b = backend.transcribe(crop)
    compared = reconcile([a, b])
    output = a.to_dict()
```

结果包含 `schema_version`、`status`、固定 `reason`、`roi_contract`、`input_sha256`、固定版本/模型/provider 身份及 `timing_ms`。文字保留原始 Unicode 与两端空白，`transcription` 只在开发候选中非空；低分、空白等诊断可能保留 `raw_transcription`，**诊断文字不能作为有效候选值**。分数是上游 raw score，不是正确率，也不能跨引擎决胜。默认 0.5 是未经真实数据校准的工程过滤值。

`Transcription.to_dict()` 的所有结果固定含有：

```json
{
  "evidence_kind": "transcription_only",
  "geometry_evidence": false,
  "text_boxes": null,
  "human_annotation": false,
  "live_use_authorized": false,
  "eligible_for_geometry_gate": false,
  "eligible_for_policy": false,
  "eligible_for_execution": false,
  "accuracy": null
}
```

| 情况 | 明确结果 |
| --- | --- |
| 一个非空合法转写，raw score 达阈值 | `development_candidate / transcription_available`，保留原字符串，不解析成游戏值。 |
| 通过原始结构校验后的空白、低分、空列表或多转写 | `unknown`；`transcription=null`，不得自动填 0、空槽或上次结果。多转写也会终止 worker。 |
| 真实后端的错误类型、畸形容器/元素、非法分数或意外框 | worker 返回 `error / invalid_backend_output` 并结束；不发送未经校验的 raw 结果。父端仍独立校验管道里的类型、分数/耗时与几何，畸形 raw 也无候选并终止 worker。 |
| 配置、文件或模型缺失，预热未完成 | 配置/启动/预热抛固定 `CandidateError.code`；未启动或未预热的转写返回 `error`。 |
| 编码/几何错误、推理异常、超时、序号/输入绑定错误 | `error`，无候选；消毒异常信息并终止 worker。后续调用返回未启动，不自动恢复。 |
| 同一裁切两个及以上结果不一致 | `reconcile()` 返回 `unknown / transcription_conflict`，文字和分数均为 null；不选最高分。 |
| 对照含未知/错误，或输入摘要/区域约定不一致 | 分别返回 `comparison_incomplete`、`comparison_input_mismatch`；没有候选。 |
| 同一裁切全部一致 | 仍只是 `development_candidate / same_crop_agreement`，不提升授权、几何或标注等级。 |

请求串行，管道交换有长度限制、请求序号与输入摘要绑定。默认每次交换最多 30 秒，等待请求锁最多 30 秒，二者可能相加；配置允许 `(0, 60]` 秒。这里没有实时服务级别保证、TTL、取帧或结果新鲜度机制。`reconcile()` 只对同一原始裁切字节做保守对照，不对不同倍率、不同区域或另一个引擎做自动融合。

## 5. 命令、验证与复现

在项目根目录使用现有 Python；下列命令只需标准库：

```sh
PYTHONPATH=src python3 -B -S -m jcc_rapid_ocr_candidate --help
PYTHONPATH=src python3 -B -S -m jcc_rapid_ocr_candidate --synthetic-check
PYTHONPATH=src python3 -B -S -m unittest discover -s src/jcc_rapid_ocr_candidate/tests -v
```

可选真实模型预热，三个路径都必须已存在；命令不会安装或下载：

```sh
JCC_OCR_ENV='/absolute/audited-venv'
JCC_OCR_WHEELS='/absolute/reviewed-wheels'
JCC_OCR_REC='/absolute/local-models/PP-OCRv6_rec_small.onnx'
PYTHONPATH=src python3 -B -S -m jcc_rapid_ocr_candidate \
  --environment "$JCC_OCR_ENV" --wheelhouse "$JCC_OCR_WHEELS" \
  --rec-model "$JCC_OCR_REC" --warmup-only
```

单裁切转写须把 `--warmup-only` 换成显式的 `--crop /absolute/crop.png --input-sha256 完整摘要 --roi-id 区域名 --width 精确宽度 --height 精确高度`，可加 `--repeat 24`。默认 stdout 省略 OCR 原文；只有显式添加 `--include-transcription` 才输出本地机器原文。该原文与图片同属私人实验资料，不应加入公开仓库。CLI 退出码：成功/预热为 0，明确错误为 2，未知/冲突为 3；重复中的错误会停止循环。

可选的真实库冒烟仍只生成合成空白图，不读取历史图：

```sh
PYTHONPATH=src python3 -B -S -m jcc_rapid_ocr_candidate.tests.smoke_offline \
  --environment "$JCC_OCR_ENV" --wheelhouse "$JCC_OCR_WHEELS" --rec-model "$JCC_OCR_REC"
PYTHONPATH=src python3 -B -S -m jcc_rapid_ocr_candidate.tests.smoke_failures \
  --environment "$JCC_OCR_ENV" --wheelhouse "$JCC_OCR_WHEELS" --rec-model "$JCC_OCR_REC"
```

标准库测试覆盖原文保留、无几何/无授权、空白/低分/多转写、NaN/Inf、冲突、错输入、资源限制、假后端、超时失效、无自动重试、未知错误消毒、wheel/安装文件篡改、未知代码拒绝及 PNG 校验顺序。新增回归通过实际 `worker.main()`、请求解码、裁切校验、`infer()` 和返回编码路径，验证畸形返回与畸形预热明确失败，而非只调用 `normalize_raw()`。独立历史模式回归包含同一合成 PNG 重复、换名复制、同一来源改名、绑定错配及四个不同输入/不同声明来源的正例。负向冒烟实际验证不受禁网策略的 worker 拒绝启动、缺/坏/同尺寸错误模型、缺 wheel、损坏 PNG、错尺寸、未应用 EXIF，以及失败后不复用 worker。临时文件放入权限私有的临时目录，结束即删除；可用 `TMPDIR` 指定已有私有目录。

2026-11-02 的路径修复后，标准库测试分别使用 Python 3.11.16、3.14.7，在 macOS 默认 `/var/...` 临时目录及其解析后的 `/private/var/...` 目录下运行，四组均为 62 项通过。独立 Agent 隔离复核追加 Python 3.9.6，两种路径下三版本共六组均为 62 项通过，见[路径复核记录](../../src/jcc_rapid_ocr_candidate/evidence/path-alias-validation-20261102.json)。新增的七项合成检查覆盖父目录别名及符号链接环、安装目录/文件/wheel 符号链接、子目录越界和 wheel 成员路径越界。本次没有运行真实模型或读取实机画面；这些检查不增加历史样本数量，也不验证不同编码图片的独立性。原 18C 和 18D 验证证据对应当时的旧源码摘要，不代表当前文件摘要。

`smoke_offline` 另有显式历史模式，必须同时传 `--historical-manifest`、`--expected-manifest-sha256`、`--baseline`、`--expected-baseline-sha256`。它接受调用方提供的兼容清单与基线，而非内置认证的18A资料白名单。验证要求如下：

- 恰好四个金币 1× 输入，裁切 ID 唯一；PNG 必须在所给清单相邻的 `inputs/` 内，实际读取的完整 PNG 字节摘要既匹配清单与基线，也各不相同。把同一 PNG 放在不同文件名下仍算重复；这不证明不同编码的 PNG 具有不同像素或局面。
- 四个输入的整数 `frame_order` 与来源帧 SHA 声明各自唯一，并逐一绑定到 `frames` 表内唯一的相同序号/来源 SHA；对应 `sample_id` 也唯一。来源路径不会被打开，来源摘要是清单声明，没有在此验证原帧内容。
- 每个 ID 的第1轮 `hot_gold` 基线记录恰好一条；输入 SHA、字段、倍率、帧序及空 slot 都必须对应，不能用字典覆盖重复记录来消除歧义。PNG 尺寸也须匹配清单及四个固定裁切的尺寸约定。

模式名为 `four_caller_bound_historical_crops_not_accuracy`。输出兼容字段 `original_input_count` 明确限定 `original_input_count_scope=unique_encoded_crop_bytes`，与 `unique_crop_png_count` 一样只计唯一编码裁切输入；`declared_unique_source_frame_count` 仅计通过绑定检查的不同来源声明。**调用方自报的清单/基线摘要只绑定本轮文件，不认证18A真源、原帧内容、真人真值或跨对局独立性。** 汇总固定给出 `manifest_provenance_authenticated=false`、`source_frames_independently_verified=false`、`independent_game_count=null`；纯合成模式的声明来源帧数为0。

通过校验后仍是3个新进程各重复6轮，四个输入共72个相关请求；重复不增加裁切、来源帧、独立对局或人工标注数量。只输出汇总耗时与机器输出一致性数量，不输出图片、OCR原文、私人路径或原图摘要。18A四图已另行核对唯一的历史结论仍保留；18D只复核原有四裁切与新校验兼容，没有新增历史OCR请求或修改18A/18C证据。复现私有路径及摘要见本机18C与18D报告，公共规格不包含实机素材。

## 6. 18C已存性能与未验证边界

下列数值保留18C当时的已存记录，不是18D新增的历史模型测试或性能承诺。18D只新增合成输入的兼容/失败检查，并确认原有四个历史裁切通过修复后的输入校验。

最终复测使用18A既有四个裁切、72 次相关请求：热态管道请求 p50 **6.24 ms**、p95 **7.39 ms**，范围 **5.74–9.71 ms**；3 次新进程含逐文件验证的启动范围 **2.334–2.557 s**，另需 **35.6–38.1 ms** 合成预热。独立的纯合成轮另有 18 次请求，启动范围 **2.351–2.538 s**，预热 **35.8–39.5 ms**。这些启动值从创建子进程计到 ready，父端解释器指纹校验及父进程导入不在其中。

先前调试轮在同四裁切上曾观察到 **27.04 ms** 的请求峰值，另一个合成轮启动曾达 **3.001 s**；保留在[汇总验证记录](../../src/jcc_rapid_ocr_candidate/evidence/validation-20261102.json)，不把最终轮的最大值当作上界。逐文件校验使启动路径与18A的轻量初始化不同；不要拿初始化小数毫秒当模型可用时间。

请求耗时包含父进程编码/摘要、管道、PNG 校验/解码、官方识别及返回校验，**不包含取帧、整帧读盘和裁切、策略、显示或设备操作**。新进程不等于清空磁盘缓存；本机未排他占用，p95 为 nearest-rank，四个裁切和重复次数不增加独立样本量。全轮与18A的机器输出一致只表示复现，人工标注仍为 0，准确率仍为 null。

没有验证商店名称的新后端性能、实时流、其他分辨率/布局、跨对局准确率、贴边/遮挡、字段身份、版本绑定、建议效果或上分能力。该原型不能进入现有允许几何门控、正式策略或执行的接口；后续任何接入须由总控另行审查转写契约和独立证据，目前没有相应授权或实现。
