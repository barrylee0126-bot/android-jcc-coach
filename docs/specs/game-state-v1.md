# game-state-v1：局面与样本契约

开发08，2026-11-02。实现依据：本地 `资料/总控/02_下一阶段开发规格_v0.1.md` 的第 3、4、6 节。
本交付只有合成局面、合成标注占位和离线契约测试；**本任务采集的真实画面、真实标注、验收场次均为 0**，没有识别准确率、真实时延或对局效果成绩。

## 1. 文件和使用方式

- [`../../schemas/game-state-v1.schema.json`](../../schemas/game-state-v1.schema.json)：主入口校验局面；`#/$defs/sample_annotation` 校验一条 `sample-annotation-v1` 标注。全部 `$ref` 为本文件内部引用。
- [`../../schemas/examples/game-state-v1.example.json`](../../schemas/examples/game-state-v1.example.json)：数字和位置均为虚构，覆盖四种状态及商店的未知、棋子、空格。
- [`../../schemas/examples/sample-annotation-v1.example.json`](../../schemas/examples/sample-annotation-v1.example.json)：没有关联图像的合成占位；全部标签未知，固定放入 `quarantine`。
- [`../../src/jcc_device/game_state_contract.py`](../../src/jcc_device/game_state_contract.py)：结构、引用、时钟、过期、相邻快照和标注分集检查；不导入视觉或设备模块。
- [`../../tests/test_game_state_v1.py`](../../tests/test_game_state_v1.py)：合成正反例，包括晚到结果、场景切换和验收集泄漏。

Schema 使用 [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12/json-schema-core)；语义规则需与结构校验一起执行。Python 的 Pydantic 数据模型不能直接替代对这份 Schema 的验证。

在项目根目录执行；以下 `python` 必须指向已安装项目依赖的解释器，例如 `.venv/bin/python`：

```sh
python -m src.jcc_device.game_state_contract schemas/examples/game-state-v1.example.json
python -m src.jcc_device.game_state_contract schemas/examples/sample-annotation-v1.example.json --annotations
python -m src.jcc_device.game_state_contract current.json --previous previous.json
python -m unittest discover -s tests -p test_game_state_v1.py -v
```

Python 接口为 `validate_state(state, previous=None)` 和 `validate_annotations(records)`；失败抛出 `ContractViolation`。调用方只在校验通过后原子替换已接纳快照。批量处理可先 `make_validator()` 并以 `validator=` 复用。其他语言可直接使用 Schema，再实现下述语义检查。

项目依赖包含 `jsonschema 4.26.0`，安装项目依赖后可在 `.venv` 中使用完整校验。Python 校验器用法见 [官方文档](https://python-jsonschema.readthedocs.io/en/stable/validate/)。纯标准库函数 `semantic_errors` 只接收已经通过结构校验的数据，不能单独充当完整校验器。

本次离线验证：Schema 元校验、全部42组单元测试（含多项子测试）、两份示例的结构与语义校验均通过；项目 Python 3.11 下不依赖 `jsonschema` 的语义检查也通过。JSON/语法、文档本地链接、行尾空白和文件末尾换行检查通过。这些结果只验证契约及反例拒绝行为，不是识别成绩。

当前根目录采用默认拒绝的 `.gitignore`：`schemas/`、`tests/` 仍被忽略。本次不修改忽略规则、不暂存、不提交、不推送；总控后续集成须明确审阅并放行这些契约文件。

## 2. 快照与字段

根对象 `schema_version` 固定为 `game-state-v1`。`sample_kind` 区分 `synthetic` / `live` / `replay`，回放不能冒充在线实测。

| 部分 | 约定 |
| --- | --- |
| 来源与顺序 | `stream_id` 是一次接收流的随机代号；重连、序号重置或重新处理回放须换代号。`frame_id` 指向 `frames` 中最新的锚帧；`sequence` 是流内递增整数。 |
| 时钟与合并 | `host_clock_id` 标识同一接收进程的单调时钟域；进程重启或时钟域改变须换代号。`snapshot_revision` 在该流每次合并、失效或冲突处理后递增。 |
| 上下文 | `context_epoch` 是可视上下文代数，场景/阶段切换、弹窗覆盖、模式变化、尺寸/裁切/方向/校准变化等使相关旧字段失效时递增；不是赛季编号。 |
| 新鲜度 | `assembled_host_monotonic_ms` 是本次快照组装时间。`freshness_policy_id` 对应可追溯的字段有效期配置，同一流内不可变；具体有效期须经回放与实测确定。 |
| 帧元数据 | 每帧保留接收时间、上下文代数、实际图像尺寸、方向、已应用旋转、游戏视区和校准代号。只保存锚帧及仍被字段/候选引用的必要旧帧即可。 |

`scene`、`phase`、`mode`、`game_version`、`round`、`hp`、`gold`、`level` 和全部五格商店均必填。未实现的字段输出未知封套，不省略。帧表中的 `frame_id`、`sequence` 各自唯一；已发布帧的元数据不可改写。

| 字段 | `recognized` 时的值与单位 |
| --- | --- |
| `scene` | `lobby`、`mode_select`、`battle`、`loading`、`settlement`、`history`、`options_dialog`、`other`；`other` 也要求确有识别依据。 |
| `phase` | `preparation`、`combat`、`carousel`、`selection`、`transition`。大厅中的对局阶段可标为不适用；遮挡时返回未知。 |
| `mode` | 已核实模式键，字符串；不预设默认模式。 |
| `game_version` | `{region, season, patch, verification_ref}`，四项均须明确；`patch` 指平衡补丁，客户端版本号不能代替它。`verification_ref` 指向开发07/09可追溯核验记录。 |
| `round` | `{stage, step}`，正整数，例 `3-2` → `{stage:3, step:2}`；与场景、准备/战斗阶段分别记录。 |
| `hp` / `gold` | 画面显示的血量点数 / 金币单位，整数。此层不预设各模式的上下限，也不自动纠正异常值；是否合法由经核规则另查。 |
| `level` | 玩家等级，正整数；不是棋子星级。 |
| `affordances` | 可选的 `shop_purchase`、`shop_refresh`、`level_purchase` 布尔观察封套。场景识别不等于功能可用；缺失或未知都不能当成 `true`。 |

`scene="battle"` 不自动推出 `phase="combat"`。旧 `scene_probe` 的字符串 `unknown` 须转成 `value:null/status:unknown`，其自评置信度也须转换成下述带类型的描述。

### 观察封套

每个核心字段、商店 `content`、功能可用性和扩展字段都有：

```text
status, value, applicability, scope, source_frame_id,
observed_host_monotonic_ms, completed_host_monotonic_ms,
expires_host_monotonic_ms, method, region, reason
```

`method={kind,name,version}`，`kind` 为 `ocr/template/vision_model/manual/metadata/fusion/unavailable`。未知方法版本用 `null`。`applicability` 为 `applicable/not_applicable/undetermined`，不适用必须输出 `unknown`、空值和 `reason="not_applicable"`。

| `status` | 当前 `value` | 附加要求与消费语义 |
| --- | --- | --- |
| `recognized` | 符合该字段类型的非空值 | 适用、方法可用、`reason:null`、截止时间非空且尚未到期。表示通过当前识别规则，不代表已证明正确或最优。 |
| `unknown` | `null` | 原因非空，截止时间为空，不得携带猜测值/旧值/候选。尚未尝试可用 `not_attempted`；遮挡、动画、方法失败、布局不支持、版本未核实等分别记录原因。 |
| `conflict` | `null` | 至少两个不同值的仍有效候选，每个保留独立方法、帧、观察/完成/截止时间和区域；封套指向最新候选帧，完成时间不早于任一候选。不得靠最后完成者静默决胜。 |
| `stale` | `null` | `last_known_value` 必须保留原类型的旧值，封套保留其原帧、方法、观察/完成/截止时间；原因限 `expired/context_changed/invalidated/disconnected`。仅供排查，不用于确定性建议。 |

`last_known_value` 仅允许出现在 `stale`；`candidates` 仅允许出现在 `conflict`。`unknown` 仍引用本次评估或失效判断针对的那一帧，不声称已读出字段；第一帧到来前不构造局面，连接故障由传输日志记录。

`scope="context"` 是默认约束。非过期字段必须来自当前上下文代数。只有 `mode`、`game_version` 可设 `scope="stream"`：核验依据明确适用于整次流时可跨普通场景切换保留，仍受截止时间约束。模式/补丁重载或证据不再匹配时必须显式失效；重连后不得自动沿用上一流的版本。第三方资料的存在不能证明当前实机补丁已核实。

### 商店与扩展

`shop` 恰有五项，按 `slot:1..5` 从左至右固定顺序；重复、缺格、乱序均不通过 Schema。每格包含观察封套 `content`：

- 确认有棋子：`{kind:"unit", display_name:"…", catalog_ref:null或完整引用}`。
- 确认空格：`{kind:"empty"}`。
- 未识别出、被遮挡、身份不完整：`value:null/status:unknown`。不能用空格代替未知。

`catalog_ref={dataset_id,entity_kind:"unit",entity_id,variant}` 是特定资料版本的引用，未知时为 `null`。不能仅凭同一 ID 认定实体、变体或适用版本相同。名字已读出但映射未核实时可保留名字，并把引用留空；依赖规则身份的策略须等待映射。

商店非未知状态必须有非空区域；区域是来源帧中的商店格位置，不是操作授权。价格、锁定、星级等独立事实放进该格 `extensions`，如 `jcc:price`，各自拥有完整观察封套。仅看清“有人但名字不可读”时主 `content` 仍未知，可用另一个明确约定的扩展表达可见性。

根对象与商店对象支持命名空间扩展，键格式为 `namespace:name`。每个扩展是一个原子观察，复合值内的全部断言共用该来源和状态；部分可信的属性必须拆成独立观察。棋盘、备战席、装备、强化和选项后续应提供专属类型/样本，再由总控协调扩展；通用扩展入口不代表这些对象已经实现。消费者须主动选择认识的扩展，不能任意相信未约定的值。

## 3. 位置、时间与过期

`region` 和 `image.game_viewport` 均使用 `{x_min,y_min,x_max,y_max}`，范围 `[0,1]`、正面积，原点在**本条来源帧已解码并已旋转后的整幅图像左上角**，向右/向下递增。左上包含、右下排除；像素位置按该帧 `x*width_px`、`y*height_px` 转换并按用途取整。不是相对裁剪 ROI、游戏视区或历史固定分辨率。局部 OCR 坐标要先变换回整帧；无法校准就用空区域并返回未知，不生成可操作目标。校准变动须新上下文代数。

所有 `*_host_monotonic_ms` 都是毫秒，可含小数，来自同一个 `host_clock_id`，例如 `time.monotonic_ns()/1_000_000`。不得填 Unix 时间、`time.time()` 或另一台机器的数值。UTC/本地日期可另记日志，不能用于排序或有效期。

- `frames[].host_received_monotonic_ms`：Mac 应用取得该完整解码帧的时间；可能已经有管道积压。它**不是平板显示/采集时间**，不能用它证明画面物理新鲜。
- `observed_host_monotonic_ms`：必须等于该 `source_frame_id` 的接收时间，不因 OCR、模型或缓存晚返回而更新。
- `completed_host_monotonic_ms`：本字段判断完成时间，必须位于来源帧接收与快照组装之间。过期封套保留原完成时间，失效发生时间由快照组装时间体现。
- `expires_host_monotonic_ms`：从来源观察时刻及字段策略确定的截止时间，必须晚于观察时刻；当前单调时间 **大于或等于** 截止时间即过期，重复推理不能续期。同一流的策略配置不可变。
- `device_capture_time`：只有可靠取得设备采集元数据时才填 `{clock_id,ticks_ms,provenance}`，否则为 `null`。不得把接收时间复制过来。v1 没有设备/主机时钟映射，禁止相减或跨域排序；即使数值相近也无效。

置信信息可省略或为 `null`；非空时明确区分 `raw_score`（数值及原始刻度）、`model_self_report`（数值或文字及刻度）、`calibrated_probability`（0–1及 `calibration_ref`）。未经适用数据集校准的 OCR 分数与模型自评不代表统一正确率，不能互比或单独解除冲突。校准引用需能追溯样本、方法与适用画面条件，本交付没有真实校准结果。

消费者读取缓存时仍要用**当前同域单调时间**检查有效期。校验器检查的是快照组装时的自洽性，不能让已保存很久的快照重新有效。TTL 的具体毫秒数、画面缓冲积压、断连、场景代数是否正确由开发10实现和实测；示例的截止时间没有推荐含义。

## 4. 合并屏障：晚到结果不能覆盖新观察

开发10的合并器须保留最后接纳快照，并按以下规则提交；这里提供验证器，未修改现有取帧/OCR链路或实现实际调度器。

1. 接纳新帧时分配流内序号和接收时间，更新最新锚帧。检测到相关区域变化，即使识别尚未完成，也先把受影响字段更新为该新帧的 `unknown`。这一步建立字段的帧序号屏障；不能等慢识别返回后再失效旧值。无可靠变化检测时保守失效。
2. 场景/阶段、遮挡或布局切换时递增上下文代数，所有 `context` 字段切至新帧未知，或把已有的旧识别值标过期；不能给旧值换个新帧来源。只有明确不受影响的流级版本/模式可以保留。断连也立即撤销相关可用值。
3. 异步结果携带提交时的流、时钟、上下文、来源帧和方法。先检查这些身份，再比较**该字段最后接纳的来源帧序号**；小于屏障的结果直接丢弃，不能因完成更晚、分数更高而覆盖，未知/冲突结果同样受此约束。相同帧的互相矛盾结果转冲突；后续显式复核才可解除。
4. 运行 `validate_state(candidate, previous=accepted)`，通过后由唯一合并者原子发布，递增快照修订号；并发任务不能分别读取同一旧快照后独立发布。超过截止时间的结果直接输出过期/未知。

例：帧41已经使金币变为未知，帧40的慢模型随后报告“50金”。来源序号40小于该字段屏障41，必须丢弃；不能把“50”换个新观察时间写回。新帧中与该字段无关的已确认不变区域可保留仍有效的旧观察，但需有变化检测依据。

验证器同时拒绝锚帧/接收时间/代数倒退、帧元数据重写、同帧截止时间延长和字段消失。可选扩展或功能观察一旦发布，同流后续也要显式保留未知封套，避免删除字段时丢失屏障。`previous=None` 只用于新流初始化；不是绕开乱序检查的日常入口。该检查无法证明生产者实际看过哪帧，也不能替代传输层处理解码积压。

## 5. 真实画面标注规范

一条标注按 `#/$defs/sample_annotation` 校验，批量放在本地 JSON 数组中；`validate_annotations(records)` 再检查跨记录规则。该入口不打开图像，也不验证文件实际存在或哈希相符，采集器须另做这两项检查。

| 信息 | 必填内容 |
| --- | --- |
| 身份与分集 | `sample_id`、冻结修订的 `dataset_id`、独立录制场次 `recording_session_id`、`stream_id`、`host_clock_id`、`split`。全部用无个人信息的代号。 |
| 去重和局面 | `dedupe_group_id` 表示相同/近似画面组；`state_group_id` 表示不同局面统计分组，不用原始帧数代替局面数。 |
| 图像和环境 | `asset` 中的本地资产代号、该标注所见图像字节的 SHA-256、`local_only`、脱敏状态；`frame` 记录实际尺寸、时钟和来源；`conditions` 记录普通画面、动画、弹窗、遮挡、布局不支持等条件。 |
| 标注和复核 | `annotation_method`、无个人信息的标注者/复核者代号及 `review_status`。真实验收记录须人工标注并由不同人员复核；未解决分歧保持标签未知。 |
| 标签 | `labels` 必填全部核心字段和五格商店。每项有 `applicability`、`truth_status`、`value`、`reason`、`region`；值类型与局面相同。 |

`truth_status="known"` 仅允许适用、非空值、空原因。`unknown` 不等于答案为零/没有棋子；适用但不可辨时 `applicable/unknown/null`，是否适用也不清楚时 `undetermined/unknown/null`。大厅里的回合等确实不适用时为 `not_applicable/unknown/null/reason:not_applicable`。已知商店答案须有来源图像区域，空格也一样。

人工按实际画面标注，可参考同次录制中的相邻帧查明动画，但须记录复核依据；被遮挡的目标帧若不能独立确证，就保留未知，不能以 OCR/模型输出充当标准答案。版本标签须关联核验记录。标注者不得依据待评模型输出修改验收答案，歧义由独立复核解决。

采集与分集顺序：

1. 以真实录制场次分组，覆盖不同回合、数值、商店变化、动画、弹窗、遮挡、方向/裁切及断连重连等条件。切片、重编码或重新命名不会产生独立场次；同一次活动的关联片段共享场次代号。
2. 用文件哈希筛完全重复，再用整帧与目标区域的近似比较配合人工复核分组；金币/回合/商店等小区域改变不能被大面积静态背景掩盖。冻结去重方法、阈值和分组修订，验收集每组只保留一个代表帧。不能预先声称一个未经实测的阈值可靠。
3. 按**整个录制场次**划分 `development` / `acceptance` / `quarantine`，同场次和同去重组不能跨集合。近似跨场次画面若共用去重组，放在同一集合或隔离。批量校验能检查已给出的组号关系，不能自动发现视觉相似。
4. 冻结验收文件、标签、分集清单及校验和后再运行评测。验收集只用于最终评测；据此调参后，后续验收应使用新的独立场次。原始图像、个人信息和本地资产映射不进入公开仓库。

`sample_kind="synthetic"` 必须为 `quarantine`、合成标注、空资产，不能进入真实验收分母。真实记录必须有关联本地资产并采用人工标注。测试中用于检查分集约束的“真实记录结构”也是内存中虚构元数据，没有真实图像或人员。

## 6. 逐字段指标与首轮验收

场景、阶段、回合、血量、金币、等级及每个商店格分别统计。五格商店是五个原子样本，不把一张画面算作五张独立画面。名字、空格/棋子和所声明的资料引用按完整规范值精确比对；别名规范化须使用冻结、版本明确的映射，不能用模糊文本相似度临时判对。

离线逐帧评测须把预测与该标注的来源帧对齐；来源不匹配记协议问题和无可信结果，不能偷偷换用另一帧的答案。流式合并评测另按锚帧的真实局面及当时截止时间核验，记录是否依靠旧帧继承，不能用旧帧答案证明当前局面正确。识别单帧和完整链路分别报告。

令 `A` 为该字段全部 `applicable` 标注数，包括适用但答案未知的困难画面：

| 计数 | 定义 |
| --- | --- |
| `C` 正确 | 答案已知，预测为仍有效且来源/类型合规的 `recognized`，值精确相等。 |
| `E` 错误 | 答案已知，预测声称有效识别但值错误。结构或来源不合规另外记协议错误，不能计为正确。 |
| `U_unknown/U_stale/U_conflict` | 相应非确定状态；缺失/非法输出按无可信结果归入 `U_unknown`，并另报协议错误数。 |
| `V` 不可核验 | 答案未知而预测给出 `recognized`；不能当作正确，也不能伪称已经查明错误。 |

应满足 `A = C + E + U_unknown + U_stale + U_conflict + V`。主报告给 `C/A`、`E/A`、各类 `U/A`、`V/A` 及计数；`A=0` 输出 `null`/样本不足，不能输出100%。`not_applicable` 和 `undetermined` 单独报告数量与原因，不能静默排除困难画面。另报已知答案数；如补充 `C/已知答案数`，只能标为辅助指标，不能替代规格要求的全适用分母。

`V>0` 时 `E/A` 只是可核验部分的错误率；还应报告最坏边界 `(E+V)/A`，或补充独立标注后再判定。不得用不可核验预测证明低错误率。协议问题、过期、冲突、未知均不算正确，不因降低输出覆盖率而改动分母。

按总控规格，首轮**待验证目标**为：至少3个独立真实录制场次、100张去重代表画面、20个不同局面；核心场景/回合/血量/金币/等级各尽量有100个适用标注，分别正确率≥95%、错误率≤2%；商店另有至少100个适用格和20种商店状态，单格正确率≥90%、错误率≤2%。不足字段分别列缺口，不能借用其他字段成绩；有不可核验预测时须以其最坏错误边界检验错误率目标。阶段等新增字段照实报告，不自行编造成绩或另设已达标结论。

每份报告还要给冻结样本/标签/分集修订、方法/模型/配置版本、适用模式与版本核验状态、画面条件分组、冷启动/预热后的耗时、过期/断连/晚到事件及未完成项。用同域接收、完成、建议生成、实际可见及结果确认时间分别计时；缺少设备真实画面起点时明确不能报告从平板变化开始的完整时延。

## 7. 现有实现衔接与边界

- 开发09：提供明确 `dataset_id`、实体变体和核验记录；未知/冲突资料不能因同 ID 被覆盖。策略依赖版本时须同时确认运行版本与资料适用性。
- 开发10：本任务开始检查时，`TabletStream.read_frame()` 只返回 JPEG 字节，没有本契约要求的帧号/来源时钟/合并屏障；`observe` 只报告聚合实验计时。本任务没有修改这些模块，其他并行会话的后续实现以其交付和总控集成为准；接入时须验证积压、失效及晚到保护。
- 开发11及策略：可用字段至少要满足 `recognized`、适用、当前流/上下文、仍在有效期及所需规则映射已核实。依赖输入为未知/冲突/过期时撤销确定性建议。布尔功能可用性也不代表动作已经执行或得到用户授权。

Schema 证明结构与声明的关系，验证器检查所提供的时间、来源和相邻快照；都不能证明画面识别正确、版本核实属实、去重充分、TTL合适或真实对局有效。实际采集、校准、评测及总控集成仍待后续会话完成。
