# Shop probe v1：离线商店开发候选

本模块从调用者提供的图片字节和私有区域配置，返回固定五个商店槽位的名称、费用候选及未知原因。它使用本机 OCR；不读取设备，不写样本或标签，不接正式局面、策略或操作接口。

真实开发画面的配置、候选词典路径、截图、OCR 原文、来源映射、摘要和运行结果均应留在项目之外。本文和合成测试不包含这些真实内容。此模块未修改共享 Schema、已有识别器或其他演示。

## 可导入入口

```python
from jcc_shop_probe import probe_shop

result = probe_shop(
    image_bytes,
    layout=private_layout,
    lexicon=candidate_entries,
    ocr=local_ocr,
    expected_sha256=expected_original_byte_digest,
)
anonymous_result = result.summary()
```

- `ShopLayout` 指定准确的解码尺寸、五个有序 `SlotLayout`、至少两个不同整数倍率、分数阈值和启发式参数。没有默认真实坐标，不按分辨率自动换算。
- `SlotLayout` 包含卡片框、卡片内名称区、独立费用框和用于空槽线索的内部框。名称区和费用框不能重叠，五张卡片框不能重叠。
- `LexiconEntry(key, name, aliases, reference_cost)` 是外部候选词典。`key` 必须唯一；名称或别名碰撞保留多个候选，不根据费用挑选。`reference_cost` 只记录与读到数字的一致或分歧，不能补出遗漏费用，也不是已核规则。
- `ocr` 实现 `recognize(png_bytes)`，返回 `text/confidence/x/y/width/height` 列表。坐标以每次实际送入 OCR 的裁切、缩放图片左上角为原点。当前私有运行入口复用现有 Apple Vision worker，没有更改其代码。

`ShopProbeResult.slots` 总是按 1–5 返回。每格含 `name`、`price` 两个 `FieldCandidate`，其 `candidates` 保留单倍率提议，`variants` 保留每个倍率的候选、满足阈值的候选和原因。名称候选为词典 key，`name_options` 提供 key 和显示名称。`status=candidate` 只表示探针规则通过，所有格始终 `identity_verified=False`、`visibility=unverified`、`font_legibility=unverified`。

## 可选价格处理实验

默认不处理价格图。需要离线研究时，可显式传入 `price_processing=ExperimentalPriceProcessing(left_padding_px=private_padding, suppress_gold=True)`；私有命令行配置可选 `experimental_price_processing` 对象，包含整数 `left_padding_px` 和布尔值 `suppress_gold`。省略或设为 null 沿用原路径。扩框按原图像素向左增加，必须始终在对应卡片内且不能和名称区相交，否则在 OCR 前报错；不更改卡片、名称或其他位置。不同画面须单独标定，不能照搬私有帧的参数。

`suppress_gold=True` 在每个倍率的费用图中，把左侧 56% 内符合 `R>=95、G>=75、R>=0.92G、B<0.70G、B<0.70R` 的像素替换为该裁图暗像素的各通道中位数，暗像素定义为各通道均小于 90；颜色处理不移动像素坐标。找不到暗背景或满足颜色条件的像素时，该倍率直接保持未知，不发 OCR。参数是有限开发试验，不是通用金币检测器。

启用时，私有诊断记录实际裁框、原始缩放裁图和送 OCR 图片的 SHA-256、掩码面积／边界、背景及每倍率原始输出；这些信息可能泄露画面，应留在私有目录。匿名摘要只标记 `experimental_price_processing=true`，不会放入坐标与 OCR 原文。每倍率的置信度、贴边、格式、分歧等原门控仍生效；费用与名称仍绑定同一输入摘要。**颜色替换可能损伤数字，候选不是真值，不能直接用于购买或其他自动操作。**

## 同帧关联与来源

`source_sha256` 是传入原图的完整编码字节 SHA-256；`frame_ref` 为 `sha256:` 加同一个完整摘要。匿名摘要不包含账户、路径或样本编号。总控与其他离线演示集成时应比较完整 `source_sha256`，不能只比较显示顺序、回合、文件名或摘要前缀。

接入方必须先分别检查两侧待匹配结果中 `source_sha256` 的唯一性。同一侧相同 SHA 对应多条记录时拒绝自动匹配，不取第一条、不静默覆盖索引；由总控显式核对来源后处理。本探针不提供跨结果的自动合并器。

摘要只证明传入字节相同，不证明原录制场次独立、画面新鲜或图像正确。改变编码但像素相同也会产生不同引用。私有命令行结果另保留原 `sample_id/frame/stream_id/host_clock_id/asset_ref` 与路径，以关联冻结清单；这些导入时钟信息不被当作历史设备采集时间。

摘要、尺寸或解码失败，EXIF 方向未应用，或输入含多帧时，函数保留相应匿名字节引用，返回五个未知，执行零次 OCR。不通过旋转、缩放或挑选多帧来暗中适配其他来源。

## 候选与不确定性

每个倍率对每格执行两次 OCR：卡片整体和独立费用框。名称只接受落在指定名称区内的完整 OCR 框，卡片主体文字只用于提示卡覆盖线索。费用只接受整个 token 为非负整数、最多三位的文本；此长度是解析限制，不是游戏价格规则。不会从长句中摘取数字。

| 层级 | 状态与含义 |
| --- | --- |
| 名称／费用 `candidate` | 每个倍率均产生同一单一候选，且没有低分、边界或未解释文字等拒绝原因。 |
| 名称／费用 `unknown` | 没有候选、某倍率候选丢失／被拒绝、出现词典外文字、格式或 OCR 问题。已经产生的提议仍保留。 |
| 名称／费用 `conflict` | 提议包含多个不同值；即使部分低分也保留分歧，不选择分数较高的一侧。 |
| 整格 `candidate` | 名称与费用均为 `candidate`，未检测到覆盖线索或参考费用分歧。仍未核实真实身份。 |
| 整格 `empty_candidate` | 内部区域暗且变化小，所有卡片／费用 OCR 均成功但无字。阈值未经真实空槽验收，黑屏等仍可误触发。 |
| 整格 `occluded_candidate` | 卡片主体 OCR 出现私有配置指定的覆盖提示词。保留逐字段结果，但不输出该格可用身份。该方法不能检测所有遮挡。 |
| 整格 `unknown/conflict` | 逐字段不足、冲突，或图像与文字线索矛盾。 |

`no_text_in_roi` 不能独自证明空槽。低 OCR 分数、文字框接触裁切边界、跨名称区边界或某倍率漏读，会保留提议并降低状态。这里的分数来自 OCR 原始分值，未校准为概率；双倍率同值也不是两次独立证据。没有检测到覆盖文字不等于无遮挡；无法据这些启发式给字体清晰度或可见性作真值判断。

词典仅作精确匹配，规范化限定为 NFKC 和空白删除；不进行模糊纠错、按子串匹配、同音替换或根据参考费用强行认定。第三方混合来源词典及未知本机补丁不能升级为已核游戏规则。

## 仅显示文本候选（v1）

`probe_shop_display_text(image_bytes, layout, ocr, expected_sha256=None)` 是一个与词典身份识别分开的最小路径。它复用同一 `ShopLayout`、原图字节摘要、五个固定槽位和 OCR 框校验，但只在每张卡片的 `name_zone` 输出未分类的 `display_text_normalized_candidate`。该字段不是棋子身份，也不能推出价格、可购买性或羁绊绑定；结果中的 `identity_status`、`price_status`、`buyability` 和 `trait_binding` 恒为 `unknown`。

- 此入口只接受恰好一份 1× 和一份 2× 视图。每对视图必须绑定完全相同的输入摘要、卡片裁切、槽位号和 `name_zone` 坐标；相同倍率重复、槽位或区域不一致均返回 `unknown / invalid_related_view_contract`。两次倍率只是同帧同后端变体，绝非独立复核。
- 任何与目标名称区相交但未完整落入的文字框会使该变体 `unknown / text_crosses_roi_boundary`，不会因同区另有完整框而忽略。完整落入但刚好贴名称区内边缘的文字框也会是 `unknown / text_touches_roi_edge`，即使它未贴整张卡片的裁切边。非有限、负宽高、越裁切或畸形 OCR 响应整体按 `invalid_ocr_response` 处理；OCR 后端的普通错误为 `ocr_error`，但不吞掉进程中断或退出。单视图漏读、低分、贴裁切边、空文本、拆分为多个框和文本冲突均不会放宽为候选。
- 比较仅对 Unicode NFKC 与空白删除后的文本进行；原始 OCR 文本不要求逐字相等，不做词典、模糊或规则纠错。成功仅是 `candidate`，不是正确率或真值。
- `frame_ref/source_sha256` 是本函数收到的编码字节摘要，用于同一调用内绑定，`source_authentication=not_authenticated`。它不认证调用方的来源、场景、时间、画面新鲜度或原始录像；调用方声明的来源不可升级为证据。

该入口的摘要不含图像、私人路径、ROI 坐标或 OCR 框。真实开发画面与输出仍只保留在项目外私有目录，公开代码、规格和合成测试不记载真实帧号、时间或摘要。验证入口同下文测试命令；合成测试覆盖双倍率一致、跨区框、畸形数值、重复倍率和“同槽不同区域”拒绝。

### 原始文字安全证据（独立 v1 包装）

已发布的 `shop-display-text-probe-v1` 摘要、槽位及 `variants` 字段合同保持不变。需要向独立身份匹配器说明原始 OCR 文本是否可安全规范化时，调用结果对象的 `sanitization_evidence_v1()`，或用导出的 `display_text_binding_sha256(summary)` 和 `display_text_slot_binding_sha256(summary, slot)` 复算绑定。它产生单独的 `shop-display-text-sanitization-evidence/v1`，而不是向旧摘要隐式添加字段。

包装包含完整 supplied display summary 的 `display_summary_binding_sha256`、每个实际槽位的 `display_slot_binding_sha256`、同一 `frame_ref/source_sha256/layout_id`，及 1×、2×各自的：

- `raw_text_safety`: `safe`、`unsafe_control_or_format` 或 `unknown`；
- `contains_control_or_format`: `false`、`true` 或 `null`。

这些哈希只把包装声明关联到一份完整显示摘要和一个声明槽位；它们不覆盖或认证包装内 `variants` 的安全状态本身，不能检测安全状态被改写，也不能阻止同一显示摘要下的状态被误置到另一槽位。它们同样不认证帧来源、时间、画面或玩家。身份接入方不得把哈希当作安全状态认证，而须直接检查两倍率状态、显示摘要合同和实际接收路径。包装不含原始 OCR 文本、文字框、ROI、图片、路径或私有时间信息。缺框、跨界、低分、异常响应或无法选择唯一框时安全状态必须是 `unknown`，不能假定为 `safe`。

在名称区内选到唯一原文后，先拒绝 Unicode `Cc`、`Cf` 或 `Cs` 字符；tab、LF、CR 和零宽格式字符均使该倍率未知，理由为 `raw_text_control_or_format_character`。仅在此检查通过后才沿用原有 NFKC 加空白删除规范化，所以普通空格仍按既有规则处理。两个倍率安全并不独立，也不能单独让身份匹配放行：接入方必须同时验证显示摘要自身合同、完整摘要／槽位绑定、两倍率均为 `safe`，以及其余身份、目录和版本门控。

当唯一框分数低于门槛时，安全状态为 `unknown/null`，即使原文为空或只含普通空格；低分优先于空文本规范化失败。若同一低分原文含 `Cc`、`Cf` 或 `Cs`，控制／格式字符拒绝优先，安全状态仍为 `unsafe_control_or_format/true`。

## 私有命令行

```text
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_shop_probe \
  --config /absolute/private/config.json --output /absolute/private/results
```

路径为占位示例。实际配置与运行入口只在本机私有报告提供。配置指定明确的 sample allowlist、工作区／冻结清单／候选词典摘要，以及私有布局。命令行先核对冻结清单、绑定和每张允许资产的字节；只读取允许列表中的图片，拒绝逃逸到工作区外的资产路径。不遍历其他数据集，不修改工作区、清单、标签或分集。

输出目录必须在项目和来源工作区之外，并且必须全新、尚不存在；祖先目录必须已经存在。输出 `results.private.json`、`summary.json`、本机静态 HTML 查看页和最后写入的 `COMPLETED.json`。页面内嵌本轮已验证 PNG 字节，并在嵌入前再次核对其 SHA，不通过 `file://` 重读可变原图；运行后替换原图或改为符号链接不会改变页面图像。没有网络资源。私有结果含真实 OCR、区域配置和精确来源映射；`summary()` 不含 OCR 原文或区域。即使是不含原文的真实摘要／候选结果也不能自动进入公开仓库。

私有结果顶层 `revision=1` 表示本输出格式修订；`summary.source_workspace_revision` 是来源工作区修订，两者不能混用。摘要还保留来源工作区／清单／配置 SHA、唯一 `run_id` 及本轮 UTC 生成时间。每帧 `source_sha256` 用于同帧关联。接入者须确认 `COMPLETED.json` 存在、`run_id` 一致，且其中三个文件的完整摘要与当前文件相同，再读取结果；没有完成标记的目录不能作为成功输出。

运行前后通过显式条件重新验证输入摘要；配置或来源在运行中改变时失败退出，检查在 Python `-O` 下也始终执行。输出路径逐层以目录句柄和 `O_NOFOLLOW` 检查；拒绝已有目录、目录符号链接、符号链接祖先、非本用户／root 所有的祖先及组／其他用户可写的祖先。最终独占 `mkdir`，文件用 `O_CREAT|O_EXCL|O_NOFOLLOW` 创建，不覆盖文件或跟随符号链接，也不改变已有目录权限。文件为 0600、输出目录为 0700；创建目录后写出失败可能留下无完成标记的部分文件。每请求超时由 OCR 后端负责；这不是整帧硬时限或实时承诺。

## 验证与计数

```text
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python \
  -m unittest discover -s src/jcc_shop_probe/tests -v
```

测试全部使用合成图片和假 OCR，覆盖固定槽位、严格字节绑定、尺寸／方向／坏图片、名称和费用候选丢失、名称／费用分歧、词典碰撞、低分、边界、词典外文字、费用不补值、OCR 失败与空槽／覆盖启发式。私有写出回归另覆盖已有目录不改权限／内容、目录及文件符号链接、可写祖先、独占写入、完成标记、嵌入图在原图替换后不变，以及 `-O` 子进程中分别改变工作区、清单、词典和原图时拒绝成功输出。它们不产生真实人工标注、独立复核或准确率。

真实实验应分别报告：独特原图与来源数、处理帧次、OCR 请求数、逐字段未知／冲突比例、每帧耗时及是否首次请求。所有空槽、覆盖和机器候选也应单独计数。没有独立真值时 `accuracy=null`；未知比例不等于错误率。离线处理耗时不包括设备采集、传输和建议呈现，也不等于端到端时延。
