# evaluation-v1：M1 离线逐帧测评

开发 10D，2026-11-02。依据 [game-state-v1 第 5–6 节](game-state-v1.md) 和本地《下一阶段开发规格 v0.1》第 6 节。

**本工具是只读离线计分器。随代码交付的样本与测试全部虚构，不是实机识别成绩。真实验收只纳入声明为真实、人工标注、独立复核完成且位于 acceptance 的合法标注。** 本工具不操作设备、不导入取帧/OCR/模型模块、不读取图像或本地资产映射、不下载依赖。完整 M1、时延与连续性门控验收不由本计分器判定。

## 1. 文件、环境与运行

- `src/jcc_evaluation/evaluator.py`：Python 接口、严格对帧、逐字段计数、样本缺口和报告。
- `src/jcc_evaluation/__main__.py`：本地 JSON 命令行，拒绝重复 JSON 键和非有限数。
- `src/jcc_evaluation/synthetic.py`：无关联图像的合成样本和演示。
- `src/jcc_evaluation/tests/`：仅合成测试；个别测试在内存中伪造 real 分支元数据验证资格与分集约束，绝不导出成真实成绩。

复用项目现有的 Python 3.11 和 `jsonschema`；没有新增依赖。本次验证环境为 Python 3.11.16、jsonschema 4.26.0。在项目根目录运行：

```sh
.venv/bin/python -m unittest discover -s src/jcc_evaluation/tests -v
.venv/bin/python -m src.jcc_evaluation --help
.venv/bin/python -m src.jcc_evaluation --demo
```

`--demo` 只在内存中生成 synthetic/quarantine 输入，自动采用 synthetic 模式；不可与外部输入参数混用。演示默认输出 JSON。要保存独立 JSON 与 Markdown 文件，指定两个尚不存在的本地路径：

```sh
.venv/bin/python -m src.jcc_evaluation --demo \
  --output /absolute/private/output/synthetic-report.json \
  --markdown /absolute/private/output/synthetic-report.md
```

路径为接口占位，目录须已存在。程序不覆盖现有文件，也不创建父目录；出错返回状态码 2。不要把真实预测、标注、账号、设备、资产映射或画面放进公开仓库。JSON 输出会包含无个人信息的样本代号、条件与方法版本，仍应按输入资料的本地保留要求处理。

真实验收运行示例，三个输入均为调用方准备的本地冻结文件：

```sh
.venv/bin/python -m src.jcc_evaluation \
  --annotations /absolute/private/input/full-frozen-annotations.json \
  --predictions /absolute/private/input/predictions.json \
  --run-metadata /absolute/private/input/run-metadata.json \
  --cohort real_acceptance \
  --output /absolute/private/output/acceptance-report.json \
  --markdown /absolute/private/output/acceptance-report.md
```

没有真实输入时不要把合成文件改名或更改 `sample_kind` 生成成绩。默认模式为 `real_acceptance`；以真实模式读取纯合成文件会纳入 0 条，所有适用分母为 0、比率为 `null`，明确无真实成绩。

## 2. 输入与可复现性

### 标注

`annotations` 为合法 `sample-annotation-v1` 对象的 JSON 数组。**先验证完整数组，再筛选 cohort**：调用现有 `validate_annotations`，包含 Schema、人工复核关系、同场次/同去重组不可跨集合、验收集每组一个代表和相同图像哈希的关系检查。必须把同一冻结修订的 development、acceptance、quarantine 一起交给该入口；只提供验收子集无法发现被省略的开发集泄漏。

每次运行只接受一个冻结 `dataset_id`。同一 `(stream_id, host_clock_id, frame_id)` 不允许换 `sample_id` 重复计分。混合冻结修订、重复目标帧或任何非法标注都会终止整个运行，返回 `no_scores`，不会跳过困难或不合法记录后继续出分。

`real_acceptance` 仅纳入以下声明同时成立的记录：

1. `sample_kind=real`、`split=acceptance`、`annotation_method=human`。
2. `review_status=independently_reviewed` 或 `adjudicated`。
3. `reviewer_id` 非空且与 `annotator_id` 不同。

其余合法记录保留在输入清单及排除计数中。synthetic 只纳入 `sample_kind=synthetic`；标注契约已要求它们采用 synthetic 标注、无资产并固定在 quarantine。同一报告始终只有一个 cohort，没有真实与合成的联合正确率。

这些检查核验的是声明的契约关系，不能证明复核人员确实不同、画面确实可辨、资产确实存在或 SHA-256 确实与文件一致。真实数据的资产校验、独立人工复核和冻结证据由样本流程提供；`review_evidence_ref` 记录其索引，本工具不自动打开该引用或资产。

### 预测

`predictions` 为以下数组；外层是本工具的绑定格式，`state` 内仍是未改动的 game-state-v1：

```json
[
  {"sample_id": "anonymous-sample-code", "state": null}
]
```

`null` 代表明确无输出；也允许完全不提供某个样本的预测。两者都保留标注分母。外层每条只能有 `sample_id`、`state` 两个键，无法确定绑定的输入整体报错；每个已绑定 state 可为非法输出，计入对应样本的 unknown 和协议错误。

- 同一 `sample_id` 有多条预测：不按值、置信度或时间挑选，整条样本无可信结果。
- 预测绑定不在完整标注清单中：记运行级 `unbound_prediction_records` 和警告，不创造分母或正确数。
- 绑定到已被 cohort 排除的合法样本：统计 `ignored_records_for_excluded_samples`，不参与本次成绩。

每个 state 先执行现有 `validate_state` 的结构及单快照语义校验。缺字段、类型错误、商店缺格/乱序/重复、非法状态/区域/时间、到期仍声称 recognized 等均使**整个快照**失去可信性，全部适用字段进入 unknown。该策略比局部抢救更保守，协议合法性未明确前不给局部正确分。

Python API 也只接受实际 JSON 类型（有限数、字符串键、对象、数组、布尔和空值）；例如 Python tuple、set、数字对象键、NaN/Infinity 都是输入错误，没有成绩。命令行还会拒绝文件里的重复对象键与数值溢出，不能借解析器覆盖前一个同名键改变结果。

### 运行修订

`--run-metadata` 可提供以下全部或部分字段，值为冻结修订字符串或 `null`：

```json
{
  "annotation_revision": "labels-frozen-revision-code",
  "split_revision": "split-frozen-revision-code",
  "dedupe_revision": "dedupe-method-and-groups-revision-code",
  "review_evidence_ref": "independent-review-evidence-index-code",
  "recognizer_revision": "recognizer-revision-code",
  "model_revision": "model-revision-or-no-model-code",
  "config_revision": "configuration-revision-code"
}
```

未提供项在报告里明确为 `missing_run_metadata`；不得据此声称已完成来源充分的最终验收。模型、方法和配置若不适用，可用有定义的 `no-model` 等冻结说明值，不应冒填模型版本。

报告记录 evaluator 版本、Python/jsonschema 版本、Schema 字节摘要、完整标注/预测/运行元数据的 canonical JSON SHA-256。命令行另记输入文件原始字节 SHA-256。canonical 采用排序对象键、UTF-8 和紧凑分隔；数组顺序保留，整数与 `1.0` 的字节摘要可不同。数值相等判定遵循下节规则。输出不含当前时间或随机数，同环境同输入的结果可重复；数据摘要本身不证明采集/冻结流程属实。

## 3. 严格逐帧对齐与状态

本版本只评单帧识别，尚不评连续流或旧帧继承。预测必须同时满足：

- 流、主机时钟与标注完全相同；合成标注只能配 synthetic 状态，真实标注只能配 live/replay 状态。
- 锚帧等于标注目标帧；frames 中该帧的**完整元数据**精确等于标注 frame（包括 sequence、上下文代数、接收时间、设备采集元数据、尺寸/方向/旋转/坐标与校准）。仅帧号相同不够。
- 被评分的每个观察封套 `source_frame_id` 都是目标帧；conflict 的每个候选也必须来自目标帧。

根身份/帧元数据不符使全部字段归为 unknown + 协议错误；合法快照中只有某个字段或某个冲突候选引用另一帧，只把该字段归入 unknown + 协议错误。即使旧帧值碰巧与目标标签相等，也没有正确分。合法 stale 如果引用另一帧，同样按错帧处理；只有来源对齐的合法 stale 计入 stale。

新鲜度只按快照的 `assembled_host_monotonic_ms` 检查。`now >= expires` 已到期；仍标 recognized 是非法输出。合法的 `stale/expired`、`stale/disconnected` 等按 stale 计数。离线工具不会将运行机器的当前单调时间与录制时钟比较，也不会因为今天运行而使全部历史样本过期。

`previous` 不传入离线单帧校验器；快照修订、异步晚到/屏障、断连重连和当前缓存读取消费须由独立流式门控与事件测试验收，不能由本工具的对帧检查代替。契约的来源声明也不能证明生产者实际看过对应像素。

## 4. 字段、计数和分母

只给 `scene`、`phase`、`round`、`hp`、`gold`、`level`、`shop.1`–`shop.5` 评分。mode/version 仅用于适用环境分组，其他字段不产生准确率。整个 state 必须合法，因为这些元数据关系仍是可信性的基础。

每个字段以标注的 `applicability=applicable` 数量为 `A`，包含答案不可辨的困难画面。计数如下：

| JSON 键 | 符号 | 规则 |
| --- | --- | --- |
| `correct` | C | 答案已知，预测来源/结构/类型合法、仍有效 recognized，规范值精确相等。 |
| `error` | E | 答案已知且合法 recognized，但规范值不同。 |
| `unknown` | U_unknown | 合法 unknown，或缺失/非法/来源不符的无可信输出。 |
| `stale` | U_stale | 来源对齐的合法 stale，不把 last_known_value 当当前答案。 |
| `conflict` | U_conflict | 来源对齐的合法 conflict，不按候选是否有正确值给分。 |
| `unverifiable` | V | 答案未知，而预测给出合法、来源对齐的 recognized。 |
| `protocol_errors` | 重叠计数 | 以上适用格次中存在任一协议问题的数量，至多每格次一次，已计入 unknown。 |

保证 `A = C + E + U_unknown + U_stale + U_conflict + V`。所有六类主比率均除以 **A**；另报 `protocol_errors/A`、`known_answers`、`unknown_answers`。不提供能掩盖未知困难画面的替代主分母。`A=0` 时所有率为 `null`，不会产生 100% 或 0% 的伪成绩。

`V>0` 时 `E/A` 明示为部分可核验错误率，必须同时查看最坏边界 `(E+V)/A`；错误率目标用后者判断，不能靠不可核验预测证明低错误率。未知/过期/冲突不会缩小分母。

`not_applicable`、`undetermined` 各有分母外数量与标签原因；其协议问题另放 `protocol_errors_outside_denominator`，不混进适用率。JSON 内逐样本保留 outcome、标注/预测状态、原因和协议问题代码，未复制预测值或画面。

值比较不去空格、不忽略大小写、不套别名、不做模糊匹配。商店比较完整 `kind/display_name/catalog_ref`；同一 entity_id 的不同资料版本、variant 或引用空值不能判成相同。确认空格与未知不同。对象键顺序不影响相等；JSON 数字 `1` 和 `1.0` 按数值相等，布尔与数字不同（合法性先由 Schema 判定）。本版本没有可选别名映射入口。

`shop_all` 只汇总五格的原子格次与比率；一张画面仍是一个画面。每个 slot 仍单独报告，不能用其他位置的成绩覆盖表现差的位置。它不是五次独立录制。

## 5. 样本、目标和缺口

报告给输入/筛选数量、排除理由、条件分组（可重叠，不可相加）、标注的 mode/version 及其未知原因、声明的方法/模型版本、冻结来源和每字段样本量。样本覆盖按以下基础计数：

| 目标项 | 首轮数量 | 计数依据 |
| --- | ---: | --- |
| 独立录制场次 | 3 | 纳入记录的 recording_session_id 数；不验证是否真独立。 |
| 去重代表画面 | 100 | dedupe_group_id 数；真实验收已验证每组一条，仍需外部图像去重证据。 |
| 不同局面 | 20 | state_group_id 数；不能用帧数替代。 |
| 场景/回合/血量/金币/等级 | 各 100 个适用标注 | 每字段 A 单独给缺口，不借用其他字段的样本。 |
| 商店适用格 | 100 | 五格的 A 之和。 |
| 不同商店状态 | 20 | 五格全部适用、答案已知时，完整规范值不同的五格向量数；部分未知的画面不凑数。 |

计数不足一律给 shortfalls。仅重复拍摄同一组号、或只改变预测不会增加不同状态数；完整商店向量按与评分一致的 JSON 值相等规则去重。标签的 catalog_ref 若有不当变化仍需冻结标注流程纠正，计分器不验证视觉真实性。

核心五字段的数值筛选要求 `C/A >= 95%` 且 `(E+V)/A <= 2%`；阶段报告但规范未给独立数值目标，记 `report_only`。商店逐格 `C/A >= 90%` 且 `(E+V)/A <= 2%`，总商店格次需满足 100/20 的规模条件。单 slot 的 `minimum_applicable=1` 只用于避免零分母，不新增“每格必须 100 条”的要求。边界比较用整数交叉相乘，包含恰好 95%/90%/2% 的情况。

`targets.real_acceptance_verdict` 只在真实模式给 `met/not_met/insufficient_data` 的**数值筛选结果**，不是完整 M1 验收。缺少条件覆盖、真实去重/独立复核证据、冻结修订或连续性/时延实验时不能宣布完整通过；顶层 `full_m1_acceptance=not_assessed`。合成模式该字段固定 `null`，全报告标为 `SYNTHETIC_TOOL_TEST_NOT_REAL_RESULTS`。

## 6. 时延与事件边界

本输入没有状态变化事件清单、冷启动/预热分组、建议生成、玩家实际可见或动作结果确认时间，也没有可信的设备/主机时钟映射。工具**不推算**这些指标：

- `latency.status=not_measured`；冷启动、预热后中位数/P95、平板真实变化到可见建议均为 `null`。
- `event_coverage.status=not_measured`；适用、成功、超时、无结果、错误、中断和事件场次均为 `null`；保留待测的 3 场/100 事件目标。
- 场景切换、弹窗、遮挡、断连重连、过期、晚到结果的真实边界事件计数均为 `null`，不是 0。合成测试只证明对应计分规则被执行；条件标签和 stale 个数不能冒充真实事件验证。

单帧接收/完成时间的差值即使存在，也不能合并成整链路时延或伪造冷/热启动实验。当前版本明确不输出这些时延成绩，后续需独立的事件契约与实验记录。

## 7. Python 接口与错误处理

```python
from src.jcc_evaluation import EvaluationInputError, evaluate, render_markdown

report = evaluate(
    full_frozen_annotations,
    bound_predictions,
    cohort="real_acceptance",  # 合成测试必须显式用 synthetic
    run_metadata=frozen_revision_metadata,
)
summary = render_markdown(report)
```

标注/绑定/非 JSON 输入错误抛 `EvaluationInputError`，带稳定 `code`，没有报告。单条预测非法、缺失或来源不符不是删样本的理由，会返回包含 unknown 与协议问题的报告。缺少现有 jsonschema 依赖时停止，不安装、不降级成不完整校验。

命令行有效执行返回 0，即使样本不足或数值目标未达到也返回报告，由 `targets` 和 `coverage` 解释；0 不代表验收通过。输入或输出错误返回 2，并在 stderr 输出 `status=no_scores` 的错误对象。输出目录不可写等错误可能留下已写出的另一个报告文件，应以运行状态码判断整次导出是否成功。
