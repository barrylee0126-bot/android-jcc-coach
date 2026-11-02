# ROI recognition v1：局部数字 OCR 开发实验

开发10F，2026-11-02。实现为 [`roi_recognition.py`](../../src/jcc_device/roi_recognition.py)，测试为 [`test_roi_recognition.py`](../../src/jcc_device/tests/test_roi_recognition.py)。复用现有 Pillow、Apple Vision worker 和 `Frame/FrameRef/PixelROI`，没有新增依赖。

**本模块只有 `development_candidate` 与 `unknown`，不输出 `recognized`，不生成 game-state-v1 或人工标注。候选只是满足当前实验规则的机器推测，须独立人工复核；多次 OCR 一致也不能替代复核。** 实际样本配置、来源和实验计数只保存在本机被忽略的10F报告中，仓库没有实机坐标、原图、OCR 原文或可用的实机校准。

## 1. 接口与边界

```python
from jcc_device.roi_recognition import (
    ImageGeometry, FieldROI, ROILayout, FrameSceneEvidence,
    probe_frame, reconcile_variants, interpret,
)
```

| 入口 | 作用 |
| --- | --- |
| `ImageGeometry(image_size, applied_rotation_degrees=0)` | 描述调用方已经解码、已经旋转后的整帧，不执行旋转。 |
| `FieldROI(field_name, roi, scale=1, minimum_raw_score=0.5, bounds=None, syntax="bare")` | 配置一个字段实际送入 OCR 的完整裁切区域及显式实验参数。 |
| `ROILayout(layout_id, geometry, sample_kind, fields)` | 固定一组精确尺寸、旋转及来源类型的实验区域；不自动寻找或缩放游戏界面。 |
| `probe_frame(frame, geometry=..., sample_kind=..., layout=..., ocr=...)` | 图像解码一次，依次裁切配置字段并返回 `ROIExperiment`；四个数字字段始终各有一个结果。 |
| `reconcile_variants(experiments)` | 对同一来源、同一裁切和语法的至少两个缩放/分数过滤变体作保守对照。 |
| `interpret(experiment, context=None)` | 应用本帧的场景、可见性、字段身份和复核声明门控，返回场景及四个数字字段的候选或未知。 |
| `experiment.summary()` | 返回固定原因码、数量、图像几何和处理耗时；不包含候选值、原文、来源身份或图像。`accuracy` 固定为 `null`。 |

`probe_frame` 的四个字段为 `round/hp/gold/level`。低层探针明确不证明画面是对局、数字属于哪位玩家或金币位置是否与当前布局相符。其 `NumericProbe.candidate_value` 仅供本机开发检查，不能直接转成正式字段或真值。

`interpret` 的 `scene` 来自调用方的外部证据，**本模块没有新增自动 battle 场景分类器**；复述外部证据时同样只返回开发候选。未配置、缺少场景依据、遮挡或不明可见性时仍为未知。模式、补丁、阶段、商店和功能可用性不在本模块范围内。

## 2. 图像、方向与坐标

输入为 `Frame.jpeg` 中的单帧 PNG/JPEG 字节，字段名沿用现有接口。编码大小最多25 MiB，实际解码最多1600万像素。图片必须完整可解码，不支持多帧图、其他格式或尚未应用的 EXIF 方向；EXIF Orientation 必须为空或1。

`ImageGeometry.image_size`、布局尺寸和实际图片尺寸须精确相同；方向由实际宽高决定。`applied_rotation_degrees` 允许0/90/180/270，只描述调用方已执行的旋转。实际像素不能证明该声明属实，因此调用方仍须提供正确元数据。旋转声明、尺寸或 `synthetic/replay/live` 类型与布局不符时不调用 OCR。回放原图、视频流的缩小图、实时界面和裁剪后的预览不得共用校准身份；相同宽高也不保证同一布局。

所有 `PixelROI.box=(left, top, right, bottom)` 相对于**已经旋转后的完整解码帧**，左上原点，右下排除，整数且在图内。没有隐式游戏视区或固定历史分辨率。每个 OCR 输入只来自该裁切的像素，以 PNG 编码；可按1–4整数倍作最近邻放大，不引入裁切外的像素，不补边、不自动纠偏或锐化。

若 OCR 在放大图中返回 `(x, y, width, height)`，则整帧文字框为：

```text
left + x / scale, top + y / scale,
left + (x + width) / scale, top + (y + height) / scale
```

`NumericProbe.input_region` 始终是**完整 OCR 输入裁切**，`text_region` 只是文字框。即使某个文字框很小，其他输入像素仍可能影响 OCR 是否返回第二个文本框或漏字，不能把依赖范围缩成该文字框。

解释后的 `input_regions` 包含完整字段裁切和外部场景验证器声明的全部 `context_regions`；`normalized_regions` 将每个整帧像素框分别除以整帧宽高，使用 [game-state-v1](game-state-v1.md) 的 `decoded_frame_normalized_top_left` 坐标约定。外部验证器使用整帧 OCR/人工看整帧时，context coverage 也必须覆盖整帧，不能只填标题位置。代码校验区域结构，不能认证声明是否覆盖了外部验证的全部像素。

## 3. 数字规则和保守失败

每个裁切只接纳**恰好一个**合法 OCR 文本框。没有结果、多框、非法坐标/分数、框触及裁切边界、低于配置的原始分数、语法失败时返回未知。不会跳过额外文本后挑“最像数字”的结果，也不按最高分静默选值。边界检查能拒绝一部分截断，不能证明所有数字均完整或区域没有遮挡。

| 字段/配置 | 语法；以下示例全部虚构 |
| --- | --- |
| `hp/gold`，`bare` | ASCII 整数，可有负号；如 `100`、`0`、`-5`。不默认各模式上下限。 |
| `level`，`bare` | 正整数；如 `7`。不能把棋子星级默认视为玩家等级。 |
| `level`，显式 `syntax="level_label"` | 完整 `正整数级` 标签，如 `7级`；只对 level 启用，不能自动去掉其他文本。 |
| `round`，`bare` | 严格 `正整数-正整数`，如 `3-2` → `{"stage":3,"step":2}`。 |

只允许去掉两端空白；不拼接内部空格或多框，不把 `O/l` 改成 `0/1`，不提取比例、百分数、带其他标签或混合文字中的数字，不接受前导零、`-0`、小数或全角数字。每个数最多12位是资源限制，不是实机模式规则。可对标量显式配置整数 `bounds`，其来源应由调用方核验；本模块不会纠正越界数字或补充默认上下限。

`minimum_raw_score=0.5` 是未校准工程过滤参数，不是正确率阈值或概率。OCR confidence 保留为原始分数，不能用于跨方法决胜或解除冲突。异常转换为固定 `ocr_failed` 等原因码，结果不复制异常原文或 OCR 原文。

## 4. 场景依据与多变体对照

`FrameSceneEvidence` 必须与实验的**完整 `FrameRef`**、几何、来源类型和布局身份相同，有非空 `verification_ref` 及明确的 context coverage。

- 合成实验只接纳 `review_status="synthetic"`；其证据不能用于真实回放或实时帧。
- 真实来源的场景依据须声明为 `independently_reviewed`；Agent/机器初步判断必须使用 `development_candidate`，解释时保持全部字段未知。代码只能检查声明，不能证明复核真实发生。
- `scene=None`、`unobscured=None/False` 均不解释数字。非 battle 场景不把可见数字当作对局字段，也不自动输出“不适用”。
- `verified_fields` 须逐字段列明已经核实该布局区域的身份；只有 battle 场景还不足以判定某个数字是玩家金币、目标玩家血量或等级。默认空列表不会解释任何数字。

即使上下文已经独立复核，OCR 数字答案仍未经独立复核，因此所有成功结果依旧是 `development_candidate`。`LocalObservation` 的 unknown 不携带候选值；低层语法候选只保留在分开的开发实验对象中。

变体对照要求同一来源、布局身份、区域、语法和 bounds；不以另一个裁切偷偷替换失败区域。所有变体有相同候选才保留候选；任一未知为 `variant_incomplete`，值不同为 `variant_disagreement`，两个原因都返回 unknown/null。不会按分数、缩放倍率或完成先后挑选。缩放使用同一 OCR，属于相关实验，不是独立方法复核；全部一致也可能全部读错。

## 5. 可重现命令和测试

项目根目录、现有 `.venv`：

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_device/tests -p test_roi_recognition.py -v
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_device.roi_recognition --help
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_device.roi_recognition --synthetic-demo
```

前两条不启动设备或模型。最后一条在内存生成11个虚构数字画面，启动本地已有 Apple Vision worker；不连接平板、不保存图像、不安装依赖。它覆盖正常整数/回合、单字符、负号、比例、混淆字母、非法回合、零等级、空白和双文本框。输出只列合成输入的候选匹配/候选差异/未知次数及耗时，不输出实机准确率。字体依赖 macOS 自带 Arial；其他主机缺少字体/worker时返回固定失败原因，不自动下载。

22个自动测试全部使用虚构像素和假 OCR，覆盖严格解析、精确裁切和缩放逆映射、完整输入覆盖、尺寸/旋转/来源类型不匹配、EXIF、损坏图、缺配置/方法、低分、多框、边缘截断、异常脱敏、场景/可见性/字段身份缺失、合成证据不能授权真实来源、错帧证据和变体冲突。测试中标记 replay 的对象也是虚构元数据，不是实机样本。

## 6. 与连续性、样本和测评的关系

本模块没有修改共享 `StatePipeline/ExactPixelContinuity`、Schema、样本工具或测评器，没有接入默认 CLI，没有操作平板、匹配或策略动作，也没有提供10E的实机场景验证器或 `RegionCalibration`。

`FrameRef` 保留调用方原身份；处理耗时使用本机 `perf_counter`，不恢复回放原时间线。没有 TTL、建议生成/可见时间或设备时钟映射，不能报告平板画面变化到建议的时延。`reconcile_variants.processing_seconds` 是变体耗时的总和，不是并行墙钟性能指标。

后续集成须由总控协调，并继续满足 [vision-integration-v1](vision-integration-v1.md) 的逐帧上下文、完整输入覆盖、失效、水位、原来源与原截止时间检查。不能用本模块的合成证据、数值语法、像素相同或变体一致代替这些条件。

真实实验只读取已经交付的本地 quarantine 样本，不能改写标注或移入验收集。源图片、机器候选值和初步据图判读都不能冒充人工真值。真实独立标注/复核、跨对局验收、当前补丁/模式绑定、遮挡/弹窗/实时布局覆盖仍待完成；按 [evaluation-v1](evaluation-v1.md) 评测前必须冻结满足资格的完整清单。目前没有真实验收准确率或完整 M1 通过结论。
