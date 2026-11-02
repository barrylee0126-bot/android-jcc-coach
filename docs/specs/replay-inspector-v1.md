# 历史回放识别检查 v1

独立的本机离线检查入口，复用 Apple Vision、10F 严格数字 OCR 和 10G 场景/字段门控。
用途是对照真实历史画面与机器候选；不输出正式局面、实战建议、人工真值或实时新鲜度。
没有接入现有策略、管线、样本写入或 synthetic-only overlay。

## 使用入口

在已配置的本机双击 `tools/历史回放识别检查.command`：读取固定开发清单，重新识别，打开私有离线 HTML。
无需粘贴数据，不连接设备，不运行浏览器服务。OCR worker 在生成完毕或退出时关闭。
关闭页面即可结束查看；运行中可按 Ctrl-C 中止。

```sh
tools/历史回放识别检查.command --no-open
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_replay_demo --help
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_replay_demo/tests -v
```

默认私有配置位于 `~/Library/Application Support/jcc-replay-inspector/profile.json`。
可通过 `--profile` 指定另一份明确审查过的私有开发配置；公开源码不包含真实样本路径、ROI 或截图。
本机实际四帧、运行文件及测量记录由总控查看项目外私有运行报告。

## 私有配置与只读约束

配置版本为 `replay-inspector-profile-v1`，明确声明 `private_development_history_only`、`replay/screenshot`、
未知模式及零额外旋转。它固定工作区/冻结清单摘要、至多四个样本 ID、准确图像尺寸、显式局部线索、
四字段 ROI、1/2 倍率、依赖文件摘要、私有匿名引用密钥和项目外输出目录。
不递归发现其他数据集，不按比例套用区域，不从画面自动决定来源类型、分辨率或主体。

仅接受冻结清单与工作区对应、quarantine/draft、原标签全未知的输入。
读取前核对元数据摘要、契约、源字节/像素摘要及尺寸；处理后重新核对原始文件与依赖。
样本只以只读方式打开，不调用 SampleStore、不打开 `.lock`，不改标签、复核、分集或来源关系。
配置变化或后续人工标注会让旧配置停止运行，由总控审查新范围后再更新私有配置。

私有目录须由当前用户持有并限制组/其他用户访问；拒绝符号链接、硬链接文件、路径穿越、
项目/Git 内部及已知云同步目录。新增运行目录为 0700，产物为 0600。配置、运行数据和真实摘要均不得纳入 Git。
这些检查保护本工具的读写边界，不是对同一用户下其他恶意程序的隔离机制。

## 页面与结构化结果

每次生成一个新私有运行目录，包含：

- `report.html`：原始画面、数字输入框、中文候选、双倍率分歧、阶段/主体/版本未知、门控原因、局部 OCR 原始返回和耗时。
- `candidates.json`：供其他本机模块调用的结构化候选；无图片、原始 OCR、私有路径或真实 ROI。
- `private-results.json`：本机调试详情，含实际局部 OCR、候选和私有输入区域。
- `summary.json`：数量、耗时和只读核验，无数值答案、OCR、帧引用、样本身份或真实摘要。控制台仅输出这一级信息与固定状态码。

`candidates.json` 顶层 `schema_version="replay-inspector-v1"`，其 `frames[]` 的结构为：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | `replay-inspector-frame-v1` |
| `frame_ref` | 用私有配置密钥对原图字节做 HMAC 的稳定匿名引用，仅在该配置内稳定 |
| `source_sha256` | 原图编码字节 SHA-256，仅留本机，用于与其他结果严格同帧连接；不能公开 |
| `source` | 固定历史回放、截图、开发用途、非实时；`capture_time=null` |
| `scene` / `phase` | `status/candidate/reason`；场景开发候选不能认证阶段、遮挡或主体 |
| `unverified` | 主体、可见性、模式、版本均未知 |
| `fields` | 按 round/hp/gold/level 索引的 `status/candidate/reason/conflict/variants[]` |
| `semantic_gate` | 四字段语义结果；未提供独立逐帧复核，保持 unknown/null 与原因码 |
| `timing_ms` | 读图、图像核验、场景、数字各倍率、对照、门控和离线识别墙钟 |
| `economy` | 本帧金币候选到 jcc_economy_lab 的显式条件映射及假设扣款比较，不升级语义门控 |
| `capacity` | 经修订标记/源字节/私有配置/ROI核对的历史容量候选或未知，条件提醒无执行权限 |
| `shop` | 已完成私有运行的同图五格名称候选及遮挡/空槽/冲突状态；费用只含识别状态 |
| `independently_reviewed/advice_eligible` | 均为 false；`recognized_count=0`、`accuracy=null` |

数字冲突时汇总 `candidate=null`、`conflict=true`，各倍率候选单列，不按原始分数或倍率挑值。
两倍率属于同一 OCR 的相关方法，即便一致仍可能共同读错。
匿名引用和字节摘要只用于同图连接，不证明拍摄主体、来源独立、版本、独立复核或新鲜度。
下游应继续保留历史/候选/未知标识，不能将这个接口当成 game-state-v1、ContextEvidence 或策略授权。

同页经济比较仅在本帧金币为双倍率一致且无冲突的数字开发候选时调用已独立交付的 `jcc_economy_lab`。
映射来源为 `replay_candidate`，主体未核实；假设价格1/2/3/4/5是互相独立的比较，并非识别出的商店价格。
显式采用 `ten_gold_five_cap_hypothesis()` 的历史10/1/5参数，本局规则、计息时点和其他收支仍未绑定。
金币未知、冲突或不满足输入条件时不计算；不复用其他帧、不改变正式局面或现有策略。
容量仅从私有配置 `capacity_results` 指定、摘要固定的缓存报告读取；不重跑容量 OCR，不读取旧报告。
报告及每条结果必须声明 `implementation_revision=roi-bound-sanitized-variants-v2`、`schema_version=capacity-probe-v1`。
按本页实际图像字节摘要与 `source_frame.encoded_sha256` 精确逐帧关联，另核对固定配置摘要、布局、完整ROI、尺寸及来源。
缺报告、缺帧、重复摘要、旧修订、格式/ROI/配置不符、倍率冲突或算式不自洽均保留unknown/null，不按列表顺序匹配或沿用其他帧。
仅消费整体成功的已用容量/显示容量/空位候选；不导出已拒绝的倍率备选值，不把显示容量等同于棋子检测或等级。
空位提示重新生成固定条件文案，阶段、主体、可上场棋子和规则仍未绑定，`execution_eligible=false/automatic_action=null`。
接纳候选前还要求倍率为至少两个互不相同的非布尔1–4整数；私有配置固定倍率时严格核对顺序和数值。
结果与配置的布局名均须符合17C的 `[A-Za-z0-9_-]{1,96}` 合同，缺失、空白或格式无效时不因两边相等而放行。
诊断必须是v2固定字段集，`reason`明确存在，`confidence.basis`为约定的同一Apple Vision倍率对照；拒绝旧式pair和额外数值字段。
结果与配置的ROI及源尺寸双方均要求非布尔整数，配置摘要本身须为有效64位小写十六进制字符串。

渲染入口再次逐帧检查图像编码字节SHA、frame.source_sha256、经济与容量各自input_source_sha256及匿名引用。
图像/结果数量不符、次序被调换、缺失绑定、重复图/引用或结果跨帧时直接拒绝生成页面；容量上游已有匹配摘要也须一致。
这一消费端检查不依赖默认运行路径的列表顺序恰好正确，合成双图逆序/缺失/跨帧测试单独覆盖它。

商店从 `shop_results` 指定的私有完成目录读取：先核对固定的COMPLETED.json摘要、revision=1和run_id，
再校验其中结果JSON、summary和HTML三个文件的完整SHA，并核对结果/summary/run_id/格式修订一致。
本页与商店结果两侧的源摘要都必须唯一；只按完整source_sha256关联，缺失、重复、文件不完整或损坏均输出五格未知。
每格只保留名称候选、名称/槽位状态、费用识别状态和固定原因码；遮挡/空槽/冲突时不输出可用名称身份。
完成文件摘要只证明完整性，消费端还逐格验证语义一致性：名称与费用各自的 `candidates` 必须等于倍率提议的并集，
`status/reasons` 必须符合上游聚合规则。候选仅在每个倍率都有同一个单一、合格且无拒绝原因的提议时保留；
有分歧、漏读或拒绝时不能靠汇总 `candidate` 覆盖证据。费用汇总为空时不能显示费用候选。
每字段须有2–4个不重复的非布尔整数倍率（1–4）；同格名称/费用及五格之间的倍率顺序必须一致。
`name_options` 必须与名称提议逐一对应。整格状态还须与字段状态、presence、原因及未核参考费用检查标记一致；
整格 `candidate` 不能覆盖未知字段，暗色图像与文字线索矛盾时不释放名称或费用候选。
`occlusion=suspected`、`body_overlay_text_cue` 或整格遮挡声明中任一项出现，均只显示遮挡候选并抑制两个字段；
其余内部矛盾只显示对应格未知；五格倍率顺序互相矛盾时全部降为未知（遮挡格继续保留遮挡）。不修补或升级上游状态。
不复制原始OCR、参考费用、候选词典、源路径或区域；不按参考费用补值，费用不传给经济比较，也不产生动作。
消费端渲染同样核对商店input_source_sha256/input_frame_ref及已有的上游匹配摘要，防止跨帧显示。

## 隐私与验证范围

HTML 是脚本为空的自包含文件，原图仅以 data URI 嵌入，导航只用页内锚点。
Content-Security-Policy 拒绝脚本、联网、字体、框架、媒体、表单和外部图片；无遥测、外部资源或 HTTP 服务。
OCR 返回内容在 HTML 中转义；后端异常只保留固定错误码。真实原图未脱敏，整份报告应留在本机。

测试使用生成图片与假 OCR，覆盖只读、固定输入、未知/冲突、同帧连接、匿名稳定性、权限、链接、
目录边界、原文转义、固定错误码、结构化脱离原文以及 worker 关闭。测试不产生真实人工复核。
实际开发图运行只报告本次候选数量与离线耗时；没有准确率、P95、端到端实战时延或上分结论。
进程创建时间不是严格冷启动，离线读取时间不是历史采集时间，门控的一秒年龄只是沿用10G的工程试验参数。
内部 FrameRef 的时刻从每帧离线探针开始分配，不表示预读结束、历史拍摄或设备采集时刻。
