# 条件建议与证据原型 v1

开发 12B，2026-11-02；12D 补充跨修订生命周期修复，12E 修复拒绝输入中的未知上下文屏障。**独立的假设复盘接口，尚未接入真实固定案例、正式策略或提示页面。** 本轮实际消费的真实回放、截图和实机输入均为 0；不访问平板，不自动购买，不输出实战动作。

本层解决开发期的一项限制：不改变 [12A 正式策略](strategy-v1.md) 的 `synthetic_fixture` 门槛，也能在另一个入口中表达带来源的 `replay_candidate`，检查其同帧、范围、证据和有效期，再解释一个假设条件。它不会将回放重标为合成，不调用 `StrategyEngine`，也不向其注入已核依据。12A、09B 和 14A 的现有拒绝门槛保持原样。

## 1. 可运行样例

在项目根目录运行：

```sh
tools/jcc-hypothesis-advice
```

默认演示全部使用构造数据，依次输出正例、数值反例、遮挡、精确到期、到期后重放旧修订，以及**构造的回放形状**样例。输出中的 `constructed_replay_template` 表明它也没有真实录制来源。

唯一的例子是参数化的单笔预算条件，**并非 13A 已选定的第一条实战场景**：

```text
G = 候选金币，P = 单笔假设支出，R = 显式假设保留线
前提：候选场景 battle、阶段 preparation、主体 self
条件：G >= P，并且 G-P >= R
```

示例人为设定 G=9、P=3、R=6，得到“假设／仅供复盘：候选余额9，单笔假设支出3后余6，达到假设保留线6。预算条件不能推出应购买。”将 G 改为8，余额为5，条件不成立。3和6均是任意测试参数，没有棋子、费用、利息、经济门槛或版本规则含义。示例不选择对象，也不推断合星、阵容或装备收益。

结果还包含理由、全部候选与假设依赖、未知和反例：

- **理由**：仅检查显式参数之间的整数关系；保留线并非已核规则或最优经济目标。
- **未知**：识别正确性、己方身份、来源真实性、模式/补丁适用性、实际费用与特殊限制、对象及机会成本、实战价值。
- **反例**：G&lt;P；G≥P但G-P&lt;R；预算通过但对象没有价值或价格假设错误；新帧、遮挡、冲突、撤销、范围变化或到期。

代码只依赖 Python 标准库。启动器优先用项目 `.venv/bin/python`，不存在时用 `python3`，不安装依赖。等效入口：

```sh
PYTHONPATH=src python3 -B -m jcc_hypothesis_advice --demo
PYTHONPATH=src python3 -B -m unittest discover -s src/jcc_hypothesis_advice/tests -v
```

## 2. 独立输入与来源

公开的完整输入由代码生成，避免抄录遗漏：

```sh
tools/jcc-hypothesis-advice --example synthetic_fixture
tools/jcc-hypothesis-advice --example replay_candidate
```

`--example replay_candidate` 输出的所有数值、摘要、引用、范围和时间仍是构造数据，`source.kind=constructed_replay_template`；它不是对真实画面识别的结果。可通过标准输入验证完整回放形状：

```sh
tools/jcc-hypothesis-advice --example replay_candidate |
  tools/jcc-hypothesis-advice --input - --review-at-ms 1100 --max-frame-age-ms 1000 --max-result-ms 300
```

上述三个时间参数也是演示值，未经实机校准。真实本地候选 JSON 可由 `--input` 显式指定，仍必须提供离线截点及两个期限。命令不读取引用指向的图像、资料、设备或网络，不自动搜索文件，不保存结果。JSON 至多256 KiB，重复键、NaN/Infinity、额外成员和格式错误会拒绝。解析/读取失败返回通用原因且退出码2，不回显路径或异常原文；完成一次协议判断时退出码0，是否阻断由结果 `status` 表达。参数缺失由命令行解析器返回2。

顶层结构：

| 成员 | 合同 |
| --- | --- |
| `schema_version` | 固定 `conditional-advice-input-v1`；`game-state-v1` 不接收、不转换。 |
| `purpose` | 固定 `hypothesis_review_only`，不能设置生产/动作用途。 |
| `sample_kind` | 仅 `synthetic_fixture` 或 `replay_candidate`。`live/live_candidate/replay` 等其他值拒绝；不能通过开关解锁。 |
| `time_basis` | 固定 `offline_review_monotonic`，与历史游戏时间、墙钟及实时物理新鲜度分离。 |
| `source` | `kind / recording_ref / capture_ref / historical_capture_time`；后者未知为null，已知时保留带时区原时间。 |
| `binding` | 全部候选和依据共同引用的完整帧绑定，见下节。 |
| `scope` | `game / region / season / patch / hotfix / mode` 六项必填，未知用null，不能省略或当通配符。 |
| `claims` | 七种已声明的依赖；缺失、未知和冲突产生缺口，不能补0或推断空位。 |

`source.kind` 只能是 `generated_fixture / constructed_replay_template / private_replay_candidate`。前者配合成输入，后两者配回放候选。构造回放模板不得改名为一次真实采集来报告成绩。

所有来源使用不透明的 `synthetic:... / private:...` 引用，不接受绝对路径、URL、设备标识或图片字节。真实来源与私有图像的对应表由后续本地适配器保管。`source_ref` 可以指向09B的具体私有来源快照或核查记录；不是对09B当前值接口的放行。合成输入只允许合成引用及 `synthetic_fixture` 依据；回放候选不能复用合成依据。引用、摘要与身份标签本身仍为调用方声明，本模块不认证其真实性。

## 3. 同帧、字段角色和证据

完整 `binding` 为：

```text
stream_id, frame_id, frame_sha256, sequence, snapshot_revision,
context_epoch, review_clock_id, received_review_ms, assembled_review_ms
```

每项 claim 和每项 basis 均须与根绑定**完整相等**。同帧号不同摘要、跨帧、跨流、跨时钟、错修订、错上下文或错时间全部拒绝。`frame_sha256` 应由后续适配器从实际输入字节产生；当前只检查摘要格式与绑定相等，未拿到字节时不能宣称重算或认证过原图。当前构造摘要仅标识生成的测试断言/帧令牌。

七项 claim 的语义：

| 名称 | 允许消费的状态 | 角色 / 单位 / 可见性 |
| --- | --- | --- |
| `scene` | `candidate` | `scene_context / context / clear` |
| `phase` | `candidate` | `phase_context / context / clear` |
| `perspective` | `candidate` | `self_perspective / context / clear` |
| `gold` | `candidate` | `self_gold / gold / clear` |
| `scope_binding` | `hypothesis` | `scope_hypothesis / scope / not_visual`；值须与根scope完全相等。 |
| `hypothetical_spend` | `hypothesis` | `single_spend_hypothesis / gold / not_visual` |
| `reserve` | `hypothesis` | `reserve_hypothesis / gold / not_visual` |

`scope_binding` 是有来源的范围假设，并不表示已核实当前客户端；本版没有可信版本裁决器。真实模式或补丁尚未核实的候选仍保留这个未知。六项范围如不能明确列为假设，应填null并阻断，而不是填一个未经声明的默认值。

`scope_binding.visibility=unknown` 同样不合格：即使值仍等于完整scope，也不能消费它或把它保存为已知范围。在下述上下文历史准入条件成立时，保守地对六维范围建立丢失边界；之后恢复原范围也须跨过最后有效范围假设的代数。其他不合格可见性也按此处理。可见性未知不表示根scope中的新值已成立。

每个 claim 还含 `value / binding / observed_review_ms / completed_review_ms / expires_review_ms / basis`，可选 `machine_confidence`。金币、支出和保留线仅接受0到2^53-1的整数；布尔、字符串、浮点、负数及非有限数不被转换。价格假设不接受 `catalog_base_price` 角色，避免把基础费用直接当实付费用。

字段状态 `unknown/conflict/stale/not_applicable` 必须携带 `value=null / expires_review_ms=null`，返回解释而不计算。`recognized/verified` 等标签或 `verified=true` 额外成员拒绝。遮挡、可见性未知、单位或主体不符均阻断。`observed_gold/opponent_gold` 不会升成 `self_gold`；`perspective` 为其他已列明主体时也不满足本例。

每个 claim 至少有一条候选 basis，包含：

```text
evidence_id, status, provenance, scope, binding, asserted_value, unit,
source_ref, source_sha256, source_pointer, basis_note,
source_time, review_ref, assessed_review_ms, expires_review_ms
```

- `status=candidate` 仅表示有一条可解释的候选断言。`unknown/conflict/revoked/stale` 全部阻断。
- `provenance` 为 `synthetic_fixture/machine_candidate/caller_assumption/review_recorded`。回放的费用、保留线和范围须由 `caller_assumption/review_recorded` 明确提供假设。`review_recorded` 需审阅引用，但仍非认证或真人复核成绩。
- 每条依据的完整scope、binding、单位与断言值必须和本项相等；任何不同均阻断，不做投票或平均。重复断言ID拒绝，不按名称、编号或多源同值合并实体。
- `source_sha256`、RFC 6901指针、非空依据说明和不透明来源引用必须存在。摘要仅校验格式，指针不被解引用；因此结果持续显示 `source_not_authenticated`。
- 机器分数不参与核验等级、放行阈值或冲突裁决。分数1.0、多条一致记录、来源自报审阅均不能改变固定的 `real_rules_verified=false`。

同一会话中，`source_sha256 + source_pointer` 固定对应同一字段、断言值、单位、范围和来源性质；跨修订、未知态和新帧都保留该约束。相同内容换 `evidence_id / source_ref` 仍是同一内容，不能通过同时修改候选值和 `asserted_value` 改写历史断言。同一输入中两条记录也不能为同一来源位置声明不同含义。确有不同的来源内容时须使用其新的内容摘要；本模块仅检查声明的一致性，不读取来源字节，也不能证明摘要被正确计算。

依据状态为 `revoked / unknown / conflict / stale`，或收到通用 `evidence_changed` 事件时，保留按字段及来源内容摘要索引的失效历史。旧内容在本会话中不能恢复为候选；改名、换引用、换定位、重绑新帧或恢复旧修订均不清除历史。通用事件没有指出哪条依据改变，因此保守地使已见依据全部失效；正常的新帧事件不会单独撤销静态来源。

恢复依赖该依据的推论需要：

1. 移除已失效内容，并提供该字段在失效前未见过的来源内容摘要。只重命名文件引用或增加一个旧来源副本不算更新。
2. 提供非空、不同于被替换记录的 `review_ref`，定位新的检查/更正记录；`assessed_review_ms` 必须严格晚于本会话记录的失效时点，且仍处于该帧合法的复盘区间。通知会读取事件当下的时钟；若通知发生在时钟读取过程中，则保守地用该次读取完成时点建立边界，不能退回上次提交时间。
3. 整份输入的帧、范围、字段、单位、状态与原期限全部有效后，才能恢复。已撤销内容的屏障继续保留；其他字段的更新不能清除它。

成功恢复时，结果的 `evidence_updates` 记录 `field / previous / replacement / trust`。其中旧记录包含原 `evidence_id / source_ref / source_sha256 / source_pointer / review_ref / assessed_review_ms`、失效时点和原因；替代记录保留新来源及审阅定位。相同依据 ID 可以承载显式的新内容版本，但不得恢复旧摘要。后续适配器须能从这些引用取回原始材料、更正说明和新内容，重算摘要并核对新事实；当前原型不能认证这些输入声明，故更新链仍标记 `unverified_caller_report`，也不是人员核验成绩。没有新增输入布尔开关。

## 4. 时间、替换与撤回

`HypothesisSession` 必须显式注入时钟，并给出 `max_frame_age_ms / max_result_ms`，范围1..30000只是原型的工程上限，没有真实有效期默认值或实机标定结论。

```python
from jcc_hypothesis_advice.fixtures import FixtureClock, example_request
from jcc_hypothesis_advice import HypothesisSession

clock = FixtureClock()  # 仅合成测试时钟，当前为1100。
session = HypothesisSession(clock=clock, max_frame_age_ms=1000, max_result_ms=300)
result = session.submit(example_request())
assert result["status"] == "hypothesis_only"
assert result["advice"] is None and result["action"] is None
assert session.view()["expires_review_ms"] == 1400
clock.advance(300)
assert session.view()["analysis"] is None
assert session.submit(example_request())["status"] == "refused"
```

判断使用注入时钟当下的读数，不用局面组装时间代替“现在”。异域时钟先拒绝，不进行跨域时间比较。收到时间≤组装时间≤判断当下；字段观察时间须为本帧收到时间，完成时间在收到和组装之间；依据检查时间也须在同一复盘区间。全部原期限必须晚于完成/检查时点。

每个“帧引用、来源内容摘要”首次进入可计算提交时建立 `首次评估起点 + max_result_ms` 的上限。后续提交消费的来源中，取最新的这个上限，再与下列原期限取最小值，`now >= expiry` 即撤回：

```text
当前依赖来源中最新的首次评估上限
相同帧与来源事实组合此前已经建立的结果截止时间（如有）
本帧 received_review_ms + max_frame_age_ms
全部依赖字段的原 expires_review_ms
全部依据的原 expires_review_ms
```

计算、复制与输出封装后再读时钟，处理耗时不能续期。重复提交完全相同的当前输入只作读取。同一帧与同一来源事实只改 `snapshot_revision / assembled_review_ms`、依据 ID、来源别名、定位或审阅标签，都沿用原结果上限；依据排序、重复旧记录或删减已见来源也不能产生新上限。期限历史经过未知、拒绝或到期仍保留；即使不先调用 `view()`，直接提交新修订也会检查原上限并返回 `fact_result_expired`。新采集或首次提供不同来源内容才可能形成新的上限，仍受全部字段/来源原期限约束；并不表示来源真实性或历史游戏新鲜度得到认证。每次 `view()` 复核时钟和到期；到期或失效后清空analysis、更新链及旧来源载荷。静态JSON副本不会自行撤回。

原子替换和拒绝规则：

1. 同一实例仅用于同一素材种类、原始录制引用、流和时钟域；改变这些项须重建会话。同帧的采集引用、摘要、序号、收到时间和上下文不可重写。
2. 新输入修订严格递增；任何非重复尝试都先撤回旧推论，包括格式错误、证据冲突、字段缺失或晚到输入。同一流/时钟域内结构完整的失败修订也记为已尝试水位，包括顺序检查失败的较新修订，修正它须新修订。
3. 新帧须有更大序号和未使用的帧引用。即使字段值相同，也产生新绑定；旧输出不得展示为新帧推论。六维范围及场景、阶段、主体分别保存最后符合本合同的候选值／范围假设及其上下文代数，作为顺序锚点，**不是已认证事实**；中间缺失、未知或不可见不会抹掉历史。值改变或从未知恢复，都须跨过最后有效锚点的代数；未知期间已推进代数时可沿用该新代数。由于同帧上下文不可改写，恢复还需要相应新帧。
4. 同帧重读或中间经过未知状态，均不能延长字段的原期限或重写原观察起点；相同来源内容也不能通过改 ID、引用或定位延长期限或重写检查起点。不同内容版本可有自己的检查时间，但字段原期限仍保留。依据变化必须作为新原子快照输入，不能原地改写已发布记录。
5. 画面已变但候选尚未齐备、断连、发现遮挡、依据撤销或上下文变化时，适配器调用 `invalidate("frame_changed" / "disconnected" / "occluded" / "evidence_changed" / "context_changed")`，按下表建立持续屏障。事件不会被一个更大的快照修订号、未知中间态或失败尝试清除；多个事件的条件累积，整份替代输入通过并完成发布后才解除待更新屏障。失效内容和期限历史仍保留。本模块不自动获知外部事件。
6. 时钟倒退、异域切换或不可用后持续拒绝，须重建会话。输入和输出均复制隔离；调用方修改返回字典不会改变缓存。

上下文历史按字段独立准入，整份输入是否可发布另行判断：

- **先检查帧**：结构、同一素材来源／流／时钟域、修订与帧顺序及上下文顺序必须通过；收到≤组装≤判断当下，且尚未到帧年龄上限。过期、未来或倒置时间、错素材、重放、错流、异域及乱序尝试均不写入上下文值或丢失边界。过期或帧时间／素材不合格的输入也不能替换上一合格帧的元数据、推进其上下文代数或记录来源撤销；同流同域失败修订仍消耗尝试水位，修正须新修订。首个帧尚不合格时，水位按流和时钟域隔离保存，不能借此把帧内容／代数当作锚点或影响其他流。
- **再检查字段**：提供的上下文claim须完整绑定根帧，角色和单位正确。显式阻断状态的观察／完成时间可以为空；如填写须属于本帧；所附依据的绑定、范围、素材性质、单位及所填检查时间／期限均须有效且未到期。未知状态不消费旧断言值。合法新帧明确缺失上下文claim时沿用缺失边界规则，不补一个已知值。有值claim还须通过本字段全部候选、来源和历史检查；角色、单位、绑定、证据或期限不合格的值跳过，不升为锚点，也不冒充一次可信未知声明。
- **只保留可信丢失**：符合上述条件的缺失、`unknown/conflict/stale/not_applicable` 声明、不可见字段或已正确绑定范围中的null，均保留相应丢失边界。新帧的显式`stale`声明与一份已超过帧／字段／依据期限的输入有区别，后者不建立新边界。即使其他字段触发硬拒绝、来源历史冲突或待更新事件屏障，可信丢失仍须记录；整份被硬拒绝或被事件屏障阻断的输入只可记录丢失，不能更新或清除已有候选锚点。任一恢复仍须整份输入通过并满足原有跨代／新采集条件。

例如，A有效→下一新帧中phase合法声明未知、但gold错绑而整份拒绝→再下一帧phase恢复A原值：`context_epoch`不跨过A的代数时，返回`context_change_unannounced`；推进代数并满足其他条件后才可恢复假设推论。若中间phase本身错绑或该帧已过期，则不能据其建立丢失屏障。这里“可信”仅指通过协议与时间／历史一致性检查，仍不认证来源、识别或范围假设真实性。

| 失效事件 | 恢复条件 | 未满足时原因 |
| --- | --- | --- |
| `frame_changed / occluded / disconnected` | 不同帧引用、更大序号、更晚收帧时间及不同采集引用，所有候选完整绑定该采集。静止画面的像素可相同，单独改修订号不算新采集。 | `frame_refresh_required` |
| `context_changed` | 上述新采集条件，并推进事件前的 `context_epoch`。 | `context_refresh_required` |
| `evidence_changed` | 旧来源内容全部移除，受影响字段提供不同内容和失效之后的新检查记录；新帧本身不能代替这项更新。 | `evidence_refresh_required / evidence_update_required` |

同一内容版本再次被提交为候选返回 `evidence_version_retired`；同一来源位置改变断言返回 `evidence_content_changed`。未知或非法失效事件也撤回当前结果，并对已知当前帧建立新采集要求。上述历史均为单实例内存状态，跨进程恢复/持久化尚未实现，不能把重建实例当作旧来源重新取得授权的办法；后续适配器须保管和回放其失效/更正历史。

CLI 的 `ReplayReviewClock` 使用**调用方显式选定的离线复盘截点，加上启动后的真实处理耗时**。它不冻结时间，也不从输入的组装时间自动选择截点。输入生产者需在导入时建立单独的离线复盘时钟，保留历史采集时间与源帧，不得把历史设备/主机时钟换标签充作同域；本程序无法认证调用方是否正确建立了这个映射。离线导入与重放不能证明当时仍在操作窗口内，故所有输出固定 `historical_game_freshness_established=false`。

## 5. 输出状态与展示边界

每个结果固定带“假设／仅供复盘·未核验·不构成实战指令”，并具有：

```text
purpose=hypothesis_review_only
production_eligible=false
execution_eligible=false
real_rules_verified=false
historical_game_freshness_established=false
advice=null, action=null
```

| `status` | `analysis` | 含义 |
| --- | --- | --- |
| `hypothesis_only` | 条件推论、理由、原假设和候选依据 | 本例参数关系成立；没有买入、卖出、阵容或装备动作。 |
| `conditions_not_met` | 不成立的条件及反例解释 | 参数关系或己方准备阶段前提不成立。上下文不符时算术结果为空。 |
| `insufficient_evidence` | null | 缺字段、范围未知、遮挡、角色/单位问题、候选分歧、缺来源、依据撤销或过期。 |
| `refused` | null | 协议、实时/正式输入、同帧、范围冲突、时间、修订顺序或时钟会话错误。 |
| `expired / invalidated` | null | 原推论已撤回，不能重新显示旧文案。 |

结果带 `source` 的不透明录制/采集引用和历史时间、完整 `binding / scope`、整个输入摘要、规则修订和摘要；有效analysis保留每条候选的证据定位与时间。`evidence_updates` 默认空列表，仅在有效推论中保留与当前来源相关的更新链；撤回后为空。原图和真实来源保存在本地私有适配器，默认输出仍须按私有内容处理，不提交到公开目录。

这是一份候选接口提案，不修改共享Schema或已部署接口。14B若由总控安排接入，应新建明确的“假设／仅供复盘”展示适配器：展示前及每次刷新读取 `session.view()`，保留 `source/capture_ref`、帧摘要、scope、输入摘要与原期限；只显示受控推论文案、未知和反例。不得把本结果改成14A的synthetic输入或12A的正式Advice，也不能把推论文案塞进动作字段。新帧/断连须先调用失效入口，再等新的候选。当前未实现这个展示接入或可见耗时测量。

## 6. 验证与下一依赖

```sh
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s src/jcc_hypothesis_advice/tests -v
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s src/jcc_strategy/tests -q
```

12B 原59项、12D新增24项与12E新增19项合计 **102项**通过，在关闭第三方包加载的系统Python中同样通过；原12A的60项回归通过。测试全为构造数据，覆盖：预算正反例与零/整数边界、来源种类隔离、无生产模块导入、实时/正式局面拒绝、高置信度和自报核验不升级、六维范围、每个绑定成员、错图、角色/单位、遮挡、缺失、冲突、撤销、原期限、异域与倒退时钟、精确到期、计算中到期/失效、断连、旧修订、水位、同帧续期及输入输出副本。12D历史回归见 [test_history.py](../../src/jcc_hypothesis_advice/tests/test_history.py)：五种事件后的修订号绕过、跨帧及改名后的撤销复活、带来源更新链的恢复、九种上下文的未知中间态、同来源内容改写、同帧过期续期、已见来源组合变化，以及失效事件的当下时间/读取中边界。12E回归见 [test_context_boundaries.py](../../src/jcc_hypothesis_advice/tests/test_context_boundaries.py)：其他字段硬拒绝中的可信丢失、范围可见性未知、全部绑定成员的反例、非法值不留锚点、过期／错域／乱序输入不写边界或推进帧代数，以及合法跨代恢复。CLI覆盖完整模板往返、显式时间参数、错误/重复键/过大JSON、异常隐私与无设备入口。NaN/Infinity直接注入字段和断言，不经过需要序列化摘要的fixture修改助手，三种金额各两种非有限值均进入验证器。

这些只验证协议、整数关系和撤回逻辑，不证明字段正确率、真实建议有效性、时延或上分能力。没有进行真实回放试接、真人标注、独立人员复核、设备采集或匹配。

下一步由总控协调：

1. **13A**确定首个有证据和价值假设的场景及真正必需字段。此处的单笔预算样例不替它作场景选择；阵容和装备仍为后续目标。
2. **09C及视觉/样本环节**提供可追溯的适用范围、规则及反例，和保留原图摘要、主体、可见性、字段来源的私有固定案例。机器候选和Agent判读不能记成真人复核。现有09B不会因此提供已核当前值。
3. **总控审阅私有适配器和14B展示**，在独立回放入口试接至少一个固定案例，验证缺项解释与撤回，再记录可见计时。当前没有从game-state-v1或现有回放检查器自动转换的适配器。
4. **正式实战能力**仍需另行审阅生产接受策略、真实依据与视觉/时钟验证；不能通过本原型的标签、分数或布尔参数开启12A。

12B 初始交付新增 `src/jcc_hypothesis_advice/`、本规范、私有交接记录及独立启动器。12D、12E均仅修改该包内的 `engine.py`、新增对应回归、本规范及各自交接记录；不更改AGENTS、README、总控台账、.gitignore、共享Schema、12A或其他会话文件，不执行Git暂存、提交或推送。
