# Scene evidence v1：逐帧场景候选与字段证据

开发10G，2026-11-02。实现：[scene_evidence.py](../../src/jcc_device/scene_evidence.py)；合成测试：[test_scene_evidence.py](../../src/jcc_device/tests/test_scene_evidence.py)。复用 Pillow、现有 Apple Vision 与只读的 [10F 局部 OCR](roi-recognition-v1.md)，没有新增依赖、模型、真实默认区域或设备入口。

**本模块只输出 `development_candidate` 或 `unknown`。不输出 `recognized`、game-state-v1、人工标签、10C/10E上下文证明或操作目标。** 自动线索、Agent 看图和相关 OCR 变体一致都不是独立人工复核；当前没有已验证的真实无遮挡、己方身份、准备阶段识别器或跨帧连续性前提。

## 1. 解决的问题与保留的边界

局部数字格式合法，仍可能来自商店价格、经验、对手血量或回放正在观察的玩家。场景、阶段、可见性、字段含义和玩家归属需要分别有依据。只检查帧号还不够：相同 `FrameRef` 被错误地配上另一幅图像时，证据也须拒绝。

本模块提供两个独立层次：

- **图像线索层**：对当前图像的显式局部区域运行 OCR，生成有限的场景/阶段假设；保留实际输入依赖，不声称无遮挡、己方视角或按钮可操作。
- **字段候选门控**：核对当前帧、私有图像摘要、时钟、上下文、布局、阶段、复核声明、可见性、逐字段主体和全部输入覆盖，再解释10F数字候选。即使外部上下文声明已复核，数字本身也只是开发候选。

没有将本层接入共享管线、Schema、样本工具、测评器或默认命令。真实实验的资产、配置、来源、次数和耗时留在本机忽略报告中，公开文件不包含真实截图、玩家、设备、资产映射或原始 OCR 字符。

## 2. 接口与精确绑定

```python
from jcc_device.scene_evidence import (
    LayoutKey, CueSpec, SceneLayout, bind_frame, probe_scene, probe_numbers,
    FieldWitness, FrameWitness, ClockReading, assess_fields, summarize_fields,
)
```

| 对象/入口 | 约定 |
| --- | --- |
| `LayoutKey(layout_id, geometry, sample_kind, capture_kind, mode_key=None)` | `geometry` 使用10F的 `ImageGeometry`。明确区分 `synthetic/replay/live` 和 `screenshot/video_frame`。`mode_key=None` 表示未知；不能当成某个已核模式。 |
| `bind_frame(frame, key, host_clock_id=..., context_epoch=...)` | 绑定完整 `FrameRef`、上述布局键、同域时钟、上下文代数和本帧**编码字节** SHA-256。摘要只留在内存，不进入默认报告。 |
| `SceneLayout(key, cues)` | 显式局部线索配置。必须与本帧声明的整个 `LayoutKey` 相等；相同尺寸也不能跨来源、模式或布局复用。 |
| `probe_scene(frame, key=..., host_clock_id=..., context_epoch=..., layout=..., ocr=...)` | 解码并校验当前完整图像；局部识别后返回 `SceneProbe`。没有配置、方法或可靠输入时返回未知。 |
| `probe_numbers(frame, key=..., host_clock_id=..., context_epoch=..., layouts=..., ocr=...)` | 在**实际同一 Frame**上调用未改动的10F；单变体直接保留，多个变体调用10F保守对照。返回含独立字节绑定及处理起止时刻的 `BoundNumbers`。 |
| `FieldWitness` / `FrameWitness` | 外部逐帧证据声明，见第4节。不是本模块自动出具的真实复核。 |
| `assess_fields(scene, numbers, current=..., now=..., max_frame_age_ns=..., witness=None)` | 必须显式给出当前权威帧绑定、同域时间与帧年龄策略。返回固定顺序 `round/hp/gold/level` 的四个开发解释；未知项不带值。 |
| `SceneProbe.summary()` / `summarize_fields(...)` | 只输出固定代码、受控场景/阶段枚举、状态、数量、几何、耗时。无数字候选值、原文、图像摘要、来源/时钟/模式/布局身份或引用。`accuracy=null`。 |

`FrameRef` 没有主机时钟域字段，因此本层额外要求 `host_clock_id`。`context_epoch` 是外部调用者的失效代数声明，不与10E内部代数互相证明。尺寸、已应用旋转、来源与模式声明也不能单靠像素认证；调用者必须正确提供。布局裁切、视区或其他预处理变化须换布局身份/上下文，不能重写旧绑定。

图像摘要只用于防止混图，不证明设备来源、语义、物理新鲜度或独立场次。重新编码同样像素也产生新摘要，故会保守拒绝旧证据。对象是开发接口而非安全认证系统；调用者故意伪造复核声明或构造数据对象仍不能产生真实复核。

原10F `ROIExperiment` 不记录字节摘要，不能在事后任意给它贴上另一张图像的摘要；须从实际 Frame 重新经过 `probe_numbers`。本层不修改10F。

## 3. 线索策略、像素与依赖

`CueSpec(cue_id, roi, exact_texts=(), scale=1, minimum_raw_score=0.5)`：

- `round_header` 使用10F严格正整数回合语法，`level_label` 使用完整等级标签语法；只产生线索是否存在，不复制数字或原文。
- `shop_experience/shop_refresh`、四种明确阶段 `phase_preparation/combat/carousel/selection`，以及 `blocker_dialog/loading/settlement` 由配置给出完整、精确的固定标签。只去除文本两端空白，不做子串匹配、拼字、数字修补或模糊匹配。
- 每个区域必须恰有一个合法 OCR 框；空白、多框、错误/非有限坐标或分数、碰触裁切边缘、低分、未知标签和后端异常都返回固定未知原因。0.5是未校准的工程过滤数，不是概率。

回合标题与两种商店文字同时匹配时，场景为 `battle` **开发假设**。没有这些线索不补猜其他场景。有任意显式阻断线索时，场景/阶段均未知。回放可以展示 battle 内容，但来源仍是 replay，不能改称实时。

阶段只接受配置的明确阶段标签。商店、倒计时、棋盘、站立棋子或回合数本身都不推出 preparation；战斗时可能仍见商店。同帧多个不同阶段线索命中时为 `phase_cue_conflict`，不选其中一个。没有命中阻断文字不能证明没有遮挡、动画或弹窗；任意文字规则也能被伪造的非游戏画面满足，合成测试明确保留这种场景假设并拒绝无外部证据的字段解释。

输入限 PNG/JPEG 单帧、25 MiB、1600万解码像素，EXIF Orientation 为空或1。完整图像尺寸须与精确几何一致。所有区域相对已经旋转的整幅图像，左上原点、右下排除；不自动缩放、旋转、去黑边、找视区或校准。区域可显式1–4整数倍最近邻放大，放大后的每个输入也不得超过1600万像素；该资源限制不是时延上界。

OCR文字框逆映射为 `left + x/scale` 等。`SceneProbe.dependencies` 保留**所有尝试过的完整裁切**，包括未匹配文本、未检测到的阻断线索及其整幅输入；不能只保留命中的小文字框。`FieldDecision.input_regions` 再合并数字完整裁切、外部上下文和字段验证器声明的全部输入。归一化始终除以来源整帧宽高，遵循 [game-state-v1](game-state-v1.md) 的坐标约定。

## 4. 外部证据、主体与新鲜度

`FrameWitness` 必须提供与当前图像完全一致的 `binding`、`scene/phase`、`visibility`、`review_status`、非空 `verification_ref`、`coverage_regions`、逐字段 `fields` 及原始 `completed_monotonic_ns/expires_monotonic_ns`。

可见性为 `clear/occluded/unknown`。合成输入只能配 `synthetic` 声明；真实来源只接受 `independently_reviewed` 声明，机器或Agent的初步判读须用 `development_candidate`，会被门控拒绝。程序只能校验声明关系，不能证明独立人员实际完成复核；真实证据必须由样本与复核流程提供。

每项 `FieldWitness(field_name, role, visibility, regions)` 单独给出语义角色和可见性：

| 字段 | 允许解释为目标字段的角色 | 仍须拒绝的常见情形 |
| --- | --- | --- |
| round | `global_round` | 年月日期、战绩条目或其他形似回合的数字。 |
| hp | `self_hp` | `observed_hp/opponent_hp/unknown`；棋子血条、右侧排行榜行。 |
| gold | `self_gold` | `observed_gold/opponent_gold/unknown`；商店价格、刷新费用、其他玩家经济。 |
| level | `self_level` | `observed_level/opponent_level/unknown`；棋子星级、棋盘容量、其他玩家等级。 |

回放正在观察的玩家不自动等于己方；即使数值相同，也不能从 `observed_*` 升成 `self_*`。只有 battle 场景仍不足，外部阶段须明确为 preparation 或 combat；selection/carousel/transition 的布局尚未支持。明确阶段线索与外部声明冲突，或本地阶段线索内部冲突时拒绝。

外部 `coverage_regions` 的矩形并集必须完整覆盖全部场景输入；每个字段的 `regions` 并集必须完整覆盖该字段的整个数字 OCR 输入。程序精确检查矩形并集，一像素缺口也不接受。几何覆盖只检查声明自洽，不能证明外部验证器确实分析了这些像素或正确识别遮挡。如果外部验证看了整帧，其声明与最终依赖都须保留整帧，不能借此缩成局部连续性区域。

门控核对 scene/numbers/current/witness 的完整绑定；同帧号不同字节、跨帧、跨流、时钟、代数、几何、模式、截图/视频或布局不符均未知。处理起止及外部完成时间不能在当前同域时间之后。`now >= observed + max_frame_age_ns` 或 `now >= witness.expires_monotonic_ns` 即未知，重复消费同一证据不会续期。未知/过期不携带数字候选值。

帧年龄策略没有默认值，调用者显式给定也不代表已经校准。本层不登记跨调用期限、不处理队列或断流，不能阻止调用者重新伪造一个更晚期限的声明；正式局面的来源、原 TTL、字段水位和同帧期限不可延长仍必须由10E/共享契约校验。离线重读时间不是历史对局时间或设备采集时间。

## 5. 合成验证与复现

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_device/tests -p test_scene_evidence.py -v
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_device.scene_evidence --help
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_device.scene_evidence --synthetic-demo
```

前两条只用合成测试，不连接设备、不调用模型。最后一条在内存生成6个英文标签画面，调用本机已有 Apple Vision；不保存图像、安装依赖或下载模型。覆盖 preparation、商店仍可见的 combat、两阶段冲突、只有数字、背景界面仍可见的设置遮挡、无明确阶段标签。OCR可能漏识别；演示报告实际观察结果，不把规则测试当成真实语义成绩。字体依赖macOS已有Arial，缺字体/worker时返回固定失败码。

36项自动测试全部使用生成像素和假 OCR；模拟 replay/独立复核的元数据也只是内存夹具，没有真实图片、人员或验收记录。覆盖局部像素/缩放逆映射、完整输入依赖、错尺寸/来源/模式/布局/旋转/EXIF、错误/多框/低分/边缘OCR、隐私、阻断/未知/阶段冲突、伪造背景、字节混图、跨帧复核、同域时间、期限、对手/观察者身份、按字段可见性、覆盖并集与像素缺口、数字语法和相关变体分歧。通过仅证明这些规则被执行。

## 6. 后续集成建议与尚缺的真实前提

由总控协调的下一步接口应携带完整绑定、逐字段身份/可见性、未缩减的输入依赖及原时间，保留未知和分歧；本交付不修改任何共享入口。需先证明真实证据来源及适用范围，再接入 [vision-integration-v1](vision-integration-v1.md)。

真实战斗场景、准备阶段图标/动画、遮挡、己方/观察玩家绑定、回放与实时布局/模式匹配尚未经独立样本验证。当前局部文字策略不能为每帧出具10E要求的上下文证据。把一次人工判读复制到后续帧、将无阻断文字当无遮挡、用相关变体一致解除未知、让较旧任务覆盖新字段水位，均不在接口权限内。

整帧精确比较、TTL、预算、字段屏障和操控规则保持原约束；如果真实验证器仍依赖整帧，连续性也仍须覆盖整帧，不能因它慢就只声明标题框。本模块的处理量与OCR过滤不构成同步检查硬时限；没有逐帧真实连续性、设备积压上界、冷启动/P95、建议可见或动作时延成绩。

已调参材料只能用于开发；独立验收须按 [evaluation-v1](evaluation-v1.md) 的整个录制场次分集、去重、人工标注与不同人员复核、冻结清单后再运行。缺少模式/补丁、真实真值和独立样本时，仍是M1未完成、准确率未知。
