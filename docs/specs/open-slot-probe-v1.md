# Open slot probe v1：离线容量与备战席外观发现原型

实现入口：`src/jcc_open_slot_probe/`；启动器：`tools/jcc-open-slot-probe`。本模块实际读取显式允许的历史图片，运行容量 OCR、备战席局部纹理检测和私有外观模板匹配，生成 JSON 与自包含中文查看页。只读图片及来源索引，不连接设备、写样本标签、发布正式局面或输出动作。

这是开发期的视觉发现工具。`review_only_conditional_clue` 也不代表有效建议；`advice_eligible=false`、`automatic_action=null`、`accuracy=null` 固定不变。当前未实现主体、可操作阶段、合法棋子、适用规则、连续窗口和新鲜度识别。

## 1. 两条独立的检测路径

### 容量

每个显式配置的原图裁切直接调用现有 `jcc_capacity_probe.probe_capacity`，要求其修订为 `roi-bound-sanitized-variants-v2`。不修改 17C，不改文字语法、不修复 OCR 字符、不从等级或另一帧推算分母，也不绕过遮挡、动画、分数或区域绑定门槛。

可配置最多四个严格裁切试验。全部试验都成功且数值一致，才返回整体 `displayed_deployed/capacity/empty_slots` 开发候选；任一未知或相互矛盾，三项整体数值均为 `null`，所有试验诊断状态也裁减为未知，不公开被丢弃的数值。默认不自动搜索裁切或挑最高分结果。一个试验已包含 17C 的两个倍率；它们不是独立识别方法。

另有最多四个可选诊断裁切，每个做原色/青色分离、各两倍率 OCR。诊断仅保留文字框数、完整比例语法是否出现、两倍率是否一致和耗时，**不返回比例或差值**。即使遮挡图片在诊断里出现完整比例，诊断也不能回填容量、认证清晰或恢复候选。青色分离仅是固定像素过滤试验，不含补画或生成像素来恢复遮挡内容。

### 备战席

调用方显式给定完整备战席搜索区域、覆盖其内部的物体观察小区，以及与完整区域精确绑定的 Agent 单帧可见性声明。没有默认设备坐标、自动布局或遮挡分类器。完整区域未知、遮挡或动画未知时，全部备战席线索拒绝。

通过区域门槛后运行两类视觉计算：

1. 对每个观察小区计算相对局部中位灰度的暗像素比例和边缘比例，返回有对比度/纹理的 `object_appearance_candidate`。小区可能只包含部分物体或重复覆盖同一物体；不是占格或棋子计数。纹理、阴影、装饰和模型越界可能形成误报。
2. 用显式私有参考裁切在搜索区域做半尺寸 RGB 归一化模板匹配。阈值由私有实验配置提供，不是正确率；同一外观的重复命中不是多份独立证据。模型参考产生 `model_like_presence=appearance_candidate`，资源参考产生 `resource_like_presence=appearance_candidate`。跨类别重叠标为 `ambiguous_object`，不据此确认模型类别。参考裁切的分类是开发者/Agent 指定的未复核外观解释。

模板、源图、参考区域及其来源组只存私有配置。输出逐项记录参考原图摘要、参考区域、输入检测框、相似度及是否同帧参考。实际实验存在自匹配开发帧，必须展示这一点，不能当泛化能力。未收录的新外观、朝向、动作、颜色、缩放、暗色棋盘或遮挡可能漏检。

存在物体不代表是棋子；形似模型不代表它属于玩家、能部署、占用一个容量或当前应当上场。未匹配始终为 `unknown`，不写“没有棋子”“空备战席”或零个单位。

## 2. 输入与来源绑定

CLI 读取项目外的私有 JSON。`frames` 是最多 128 张显式图片白名单；每项必须给出绝对 `image_path`、完整编码字节 `expected_sha256`、`alias`、`source_kind`、`image_size`、`viewing_group`、`collection_activity`、`capacity_trials` 以及可选 `bench/diagnostic_trials`。重复摘要或重复别名拒绝，不接受目录扫描、glob 或设备输入。

来源种类只允许 `synthetic/replay`。图片只允许完整 PNG/JPEG 单帧，最大 25 MiB、1600 万像素、EXIF 方向为空或 1；不自动旋转。容量沿用 17C 的 `ProbeLayout/FrameEvidence`。备战席及参考图声明也使用同一 `FrameEvidence` 结构，绑定实际字节摘要、来源种类、图像尺寸、旋转、布局和**完整输入 ROI**；坐标为左上原点、右下排除。移动或缩小区域不能沿用旧声明。

真实回放 CLI 必须提供 `source_index={path, expected_sha256}`。索引中的 `frames` 每项含 `alias/source/asset_path/encoded_sha256`。程序核对图片别名、观看组、路径和摘要；参考图也必须能在同一索引中找到对应的路径/摘要/组。现有发现索引可直接使用，只读且哈希固定。报告同时保存索引及配置摘要。

索引绑定只证明与该本机索引一致，不认证历史录像真源或独立场次。`collection_activity` 是配置中的关联分组声明；不能由它计算独立录制场次数。图片哈希不是时间戳、原录制帧号、连续窗口或实时新鲜度证明。

备战席配置字段为：

| 字段 | 含义 |
| --- | --- |
| `layout_id/roi/evidence` | 私有布局及完整搜索区域的单帧绑定 |
| `object_regions` | 1–32 个包含在搜索区域内的观察小区；不是单位真值 |
| `parameters.template_min_score` | 模板相似度过滤门槛，未校准 |
| `parameters.foreground_dark_delta` | 相对局部中位灰度的暗像素差值 |
| `parameters.foreground_min_fraction/edge_min_fraction` | 纹理观察小区的过滤门槛，未校准 |

参考配置包含 `reference_id/class/image_path/expected_sha256/source_kind/viewing_group/collection_activity/layout_id/roi/evidence`；`class` 只允许 `model_appearance/resource_appearance`。API `probe_frame(payload, frame, references, ocr)` 的 `references` 是调用方已验证的内存参考裁切；CLI 负责从原始白名单字节验证并构造它们。API 本身不认证调用方构造的参考元数据。

## 3. 输出与条件线索

逐帧包含 `source_frame`、`capacity`、`bench`、`diagnostics`、`context`、`review_only_hint` 和耗时。所有路径均使用传给 `probe_frame` 的同一份原图字节；容量与备战席分别门控，不能用一个区域的清晰声明覆盖另一个区域。

| 情况 | 显示和数值政策 |
| --- | --- |
| 容量严格候选有差值 | 仅供复盘的条件线索；主体、阶段、规则、物体合法性和时间窗口仍未知 |
| 容量严格候选满额 | 仅供复盘的满额观察；不产生空位条件线索 |
| 容量未知，存在物体候选 | 说明物体外观候选和容量未知，不能判断空位或上场条件 |
| 无支持线索 | 保持未知；不沿用上一帧或把未知当零 |
| 遮挡、画面区域或来源不匹配 | 拒绝对应字段；摘要/整帧几何不符时不运行该帧识别 |

所有实际主体、阶段、规则、合法备战单位和连续稳定性字段始终未知。即使调用方提供额外的“己方备战”键，本模块也不消费它来升级结果。这里只处理单张历史图，没有实时 TTL、撤回器、最新帧水位或设备操作接口，不应直接接到 12A 正式策略。

查看页内嵌本次参与检测的同一原图字节解码所得的**原尺寸原始裁切**，框作为 SVG 覆盖，不改图像像素。每幅预览保存来源原图 SHA-256、完整裁切区域和派生 PNG 摘要；总控可重新裁切并逐像素核对。页面不包含 `file://`、外部资源、原图文件名或 OCR 原文，也不需要网络。页面可能较大，因为保留精确裁切而非压缩缩略图。

输出必须是项目外、已有 0700 私有父目录中的新目录；不覆盖目录、输入文件或符号链接，不更改其他目录权限。目录 0700、文件 0600。重新计算配置、索引及图片输入摘要后才写报告；`COMPLETED.json` 绑定配置、索引、结果 JSON、HTML 和逐帧源图摘要。完成标记是本机完整性记录，不是可信真值或独立复核签名。

## 4. 重复运行与验证

项目根目录执行；不连接平板：

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_open_slot_probe/tests -v
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_capacity_probe/tests -q
tools/jcc-open-slot-probe --help
tools/jcc-open-slot-probe --config /absolute/private/config.json --output /absolute/private/new-run-directory
```

复跑须选择新的输出目录。运行依赖项目已有 Python 环境中的 Pillow、NumPy、OpenCV，以及现有本机 Apple Vision worker；没有新下载、联网推理或依赖安装。合成测试使用生成像素和假 OCR，不启动真实 OCR worker。

本模块的 31 项合成测试覆盖：条件线索仍禁用建议/动作、满额、遮挡及动画拒绝、错帧/错区域、倍率或裁切冲突、未知不补值、资源不升级为模型/合法单位、空白不升级为不存在、诊断不能恢复遮挡数值、OCR 异常脱敏、同帧预览逐像素绑定、输入前后不变、重复帧拒绝、来源组错配、回放缺索引拒绝、旧容量 reader 修订及旋转声明不符拒绝、私有权限、符号链接和覆盖拒绝，以及识别中输入被改不能产生完成标记。17C 原有 32 项测试另行通过，不改其代码或测试。

计时分别记录文件与参考准备、新 worker 进程启动、首 OCR 请求、每次 OCR 请求和逐帧处理。逐帧处理包含摘要、解码、门控、视觉计算及所选 OCR 诊断；不含文件读取和预览编码。报告的写前耗时包含预览生成，但不含最终输入复查、输出写入及用户打开页面。不同帧实际执行的 OCR 和门控工作量不同，不能由这些耗时推断真实对局响应速度或稳定 P95。

实际开发发现及限制见 `资料/开发/17G_空位与备战席线索.md`。真实配置、坐标、比例候选、私有来源映射及运行产物全部保留在项目外。
