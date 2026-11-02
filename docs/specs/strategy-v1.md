# 策略规则骨架 v1

开发 12A，2026-11-02。依据项目开发规格、[game-state-v1](game-state-v1.md) 与 [catalog-v1](catalog-v1.md)。

本交付是**离线策略接口与拒绝判断机制**。默认规则容器为空，默认资料接口拒绝提供已核规则。唯一示例的版本、模式、对象、预算门槛与价格全部虚构；不安装真实战术，不连接设备，不生成点击、拖动或其他操作命令。`live` 和 `replay` 输入一律不能触发示例建议。它没有完成 M2 的真实画面到建议链路，也没有识别、实战收益或上分成绩。

## 1. 入口与离线复现

代码为 [src/jcc_strategy](../../src/jcc_strategy/)。使用项目现有 Python 环境及 `jsonschema`，不增加依赖；只调用局面模块已公布的 `make_validator / validate_state`，不导入识别、传输或开发 09B 的证据内部实现。

在项目根目录运行：

~~~sh
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s src/jcc_strategy/tests -v
PYTHONPATH=src .venv/bin/python -B -m jcc_strategy
~~~

第二条只运行内置合成案例，不接收外部资料或设备参数。结果给出条件建议、仍有效的复核和场景变化后的失效复核。证据定位 `synthetic://jcc_strategy/...` 及其摘要仅定位生成的合成断言，不是外部网页、实机截图或真实人工审查。

最小 Python 示例：

~~~python
from jcc_strategy.fixtures import fixture_case, next_fixture_frame

case = fixture_case()  # 显式选择完全虚构的离线测试素材。
engine = case.engine()
assert engine.observe(case.state).valid
decision = engine.evaluate(case.rule.rule_id)
assert decision.status == "conditional_fixture"
advice = decision.advice
assert advice is not None
assert engine.revalidate(advice).valid

engine.invalidate("合成画面变化事件，识别尚未完成。")
assert not engine.revalidate(advice).valid
assert engine.observe(next_fixture_frame(case.state)).valid
assert not engine.revalidate(advice).valid  # 旧建议不会因新一轮评估而复活。
~~~

## 2. 规则与统一接口

`Rule` 是不可变的声明式规则。规则不能执行回调、动态表达式或读取未声明的局面；条件由 `FieldValue / Literal / EvidenceValue` 组成，只支持精确相等及数值大小比较。JSON 字符串不执行为代码；`true` 不当作 `1`，字符串不自动转数值。

| 规则内容 | 语义 |
| --- | --- |
| `rule_id / revision` | 规则身份和修订；证据还绑定整个规则内容摘要，不能同修订偷偷换条件 |
| `category` | `lineup / trade / equipment`，三类共用同一容器与生命周期 |
| `scope` | 地区、赛季、平衡补丁、模式全部明确且精确相等；未知不作通配符 |
| `intent` | 语义建议：阵容考虑、买入/卖出/保留考虑、装备/保留装备考虑，不是动作指令 |
| `target / target_label` | 一个声明的字段及对象说明；不包含设备坐标 |
| `conditions` | 适用条件、字段值/嵌套成员、比较对象及可读解释 |
| `evidence` | 每条依据的引用与用途；至少有一条 `rule_basis` |
| `applicability / rationale / invalidates_when` | 适用说明、理由及规则特有失效条件 |
| `max_age_ms` | 建议最长有效时长；只用于工程演示，没有经过实机标定 |
| `material_kind` | `synthetic_fixture` 或 `unreviewed`；后者不能输出建议，没有生产核验开关 |

每条规则自动依赖 `scene / phase / round / mode / game_version`，再加入条件与目标使用的全部观察字段，不允许漏写依赖。`FieldValue.pointer` 指向**观察封套**，`value_pointer` 指向其值内的成员。例如 `/shop/1/content` 对应第 2 格；索引是 JSON 数组从 0 开始的索引。

只消费局面契约定义的原子观察位置：核心字段、五格商店内容、功能观察和显式命名空间扩展。扩展的真实语义与识别仍须另行核实；示例的 `fixture:*` 扩展完全虚构，不等于已实现棋盘或备战席识别。条件所需字段为未知、冲突、过期、不适用或缺失时，返回拒绝原因，`advice=null`。已知且可信的条件不成立时返回 `not_applicable`，同样没有建议；未被该规则依赖的未知血量等不会单独阻断。

普通结果只有：

| `Decision.status` | 含义 |
| --- | --- |
| `conditional_fixture` | 虚构条件全部成立的离线测试建议；`execution_eligible=false / real_rules_verified=false` |
| `not_applicable` | 至少一个可判断条件不成立 |
| `refused` | 输入、时间、来源、版本、依据或集成能力不足，不能判断 |

拒绝结果带稳定的 `Reason.code`、说明及相应字段/依据引用；不会把资料接口异常原文直接展示出来。规则容器不排序“最优”阵容，不解决多规则之间的收益、优先级或资源争用。阵容与装备只有接口，没有预装战术。

## 3. 局面绑定、当前时间与撤销

`StrategyEngine` 一个实例仅用于一条流与一个时钟域。`observe(state)` 先复制 JSON，再运行主局面契约的结构、来源、时间和相邻快照检查。失败的新输入会撤销旧建议，不静默继续展示旧结果。完全相同的当前快照可重复读取；经过失效屏障后，旧修订不能恢复为当前局面。重连或时钟域变化须重建会话，重新提供对应证据。

评估器从注入的 `Clock.now()` 读取**评估当下**的时间，不接收调用方为单次评估传入的任意 `now=`，不使用快照组装时间冒充当前时间。生产者与消费者须复用真实相同的单调时钟域；仅把字符串标签改成相同不能证明时钟同源。`MonotonicClock` 使用 `time.monotonic_ns()`，`FixtureClock` 只提供合成时钟。时钟域不匹配时先拒绝，再也不进行跨域时间比较；时钟倒退或域在会话内改变后，会话持续拒绝输出。

有效期为以下截止时间的最小值：

~~~text
评估开始的当前单调时间 + 规则 max_age_ms
全部字段依赖的原始 expires_host_monotonic_ms
全部依据的 expires_host_monotonic_ms
~~~

`当前时间 >= 截止时间` 即到期。评估完成、输出封装和签发前再次读时钟，依据等待和封装耗时不能续期。读取旧建议必须调用 `revalidate(advice)`，会重新检查当前时钟、局面、条件、证据和修订；复核不能延长旧建议的有效期。每条结果包含帧、流、快照修订、上下文代数、时间策略、字段观察原文、规则及依据摘要。

撤销采用保守规则：

1. 接纳任一新帧或新快照修订，即使值看起来相同，也使所有旧建议失效；场景、模式和版本变化同样失效。
2. 在新画面尚未完成识别、发生断连或发现遮挡时，调用 `invalidate(reason)`，立即建立失效屏障。待符合契约的更新快照到达后再评估。
3. 依据撤销、变为未知/候选/冲突/过期、引用内容变化或证据修订变化，使旧建议失效。评估期间的新局面或资料变化也阻止旧计算发布。

**本模块不会自行获知平板发生了变化，也没有计时器或显示组件。** 后续状态适配器必须在画面接收/变化时调用失效入口并发布最新快照；显示层必须在每次显示、读取和计时刷新时复核，并按截止时间撤下提示。上面的行为已在合成事件中测试，尚未接入实时传输和提示界面，静态 JSON 文件不会自行撤回。未通知评估器的外部变化无法由本模块检测。

建议对象不可变，返回字典为副本；会话签发标记可检测调用方篡改文案或延长有效期。该标记只证明当前进程曾签发相同结果，不证明游戏规则、识别正确性、来源独立性或真实效果，不是跨进程信任凭证。更换会话后旧对象失效。

## 4. 开发 09B 的显式对接边界

[evidence.py](../../src/jcc_strategy/evidence.py) 定义策略侧 `EvidencePort`，不引用 `jcc_catalog.evidence`；**没有真实资料适配器，也没有完成 09B 集成**。

~~~python
class EvidencePort(Protocol):
    def revision(self) -> str: ...
    def resolve(self, query: EvidenceQuery, *, at: ClockTick) -> EvidenceReport: ...
~~~

`revision()` 是快速、无阻塞的一致性屏障。实现方必须在任何报告内容或撤销状态改变时原子更换修订，不能原地修改已返回报告；评估开始和结束、输出封装后检查该屏障。它不表示资料已核验。`resolve` 收到精确版本/模式、流、样本类型、用途、规则修订及内容摘要；调用需要当前同域时间。

`EvidenceReport` 同时记录引用、用途、状态、素材性质、精确作用域、流/时钟域、核验时点与有效期、原子断言值、条件、可选单位和定位。定位包含资料快照、记录键、字段指针、源内容 SHA-256、定位字符串及审查引用。状态可以表达 `missing / unknown / candidate / conflict / stale / unverifiable`；它们全部阻断建议。`available` 只表示接口能提供该报告，**不等于真实规则已核验**。v1 还要求素材为 `synthetic_fixture`，即使填齐真实审查字段也不能解除 `live/replay` 的全局拒绝。

自动要求两种特殊依据：

- `runtime_binding`：从局面 `game_version.value.verification_ref` 派生，报告须明确断言本次流的完整版本与模式；“字段已识别”或“这个引用非空”不等于该依据可消费。
- `rule_basis`：绑定 `rule_id / revision / rule_digest`；更换条件、目标、依赖或解释也使旧依据不匹配。

运行版本与规则依据不能复用同一个引用 ID。商店对象还要由规则显式比较完整 `catalog_ref` 和实体映射依据；不凭同名或共用 ID 合并变体。`verified=true`、供应方 `validation.valid`、自报高置信度以及多个来源原值相同均不能解除拒绝；纯布尔核验字典不是合法证据报告。

后续真实适配器仍须完成：

1. 将开发 09B 的字段断言、源定位与摘要、版本适用性裁决、冲突及撤销映射到该边界，并验证断言内容和定位实际可复核；来源元数据不能直接升格为项目核验。
2. 证明运行客户端、地区、赛季、具体平衡补丁/热修复、模式与资料严格适配，明确单位、变体、适用条件、审查方法和过期/撤销策略。`Scope` 当前只镜像局面契约的四个维度；资料要求的游戏、主题或热修复维度无法被现契约确认时应拒绝，不能把缺项当作任意值。
3. 明确哪些依据与运行时钟同域：静态资料的采集/审查日期通常是带时区墙钟，不能直接作为本接口的单调时钟时间。接入时应保留原审查时间与来源，另在当前进程建立可追溯的有效期和撤销检查，不把时间换标签充作同步。
4. 在另外审查的生产接受策略中定义可消费的核验种类，并完成真实输入拒绝用例；本版不提供布尔开关或配置参数开启实机建议。

这些是待接入契约与工作项，不是对 09B 当前内部 API 或核验能力的描述。

## 5. 合成案例与验证边界

例子仅存在于显式导入的 [fixtures.py](../../src/jcc_strategy/fixtures.py)。`fixture-mode / fixture-patch-0`、纸片小方、2 个虚构金币的价格、7 个虚构金币的门槛、1 个持有对象和 1 个空位全部自造。在这些玩具条件成立时输出“考虑第 2 格”，只演示条件、依据、建议及失效的连接。没有关于游戏强度、合成结果、理财门槛、阵容最优性或真实可买性的断言。

离线测试覆盖作用域错误、字段未知/冲突/过期/缺失、布尔与数字区别、当前时钟及到期边界、异域/倒退时钟、依据不全/冲突/伪核验/撤销、评估中到期和资料/局面更新、场景变化、晚到快照、断连屏障、重连、新会话、篡改建议及输入输出副本隔离。测试不启动设备，不读取真实截图，不联网，不测识别准确率或响应速度。

真实数据、经核版本、五格商店与容量识别、实时失效通知、显示撤回和真实策略案例均需后续联调与实测；本交付不能替代这些验收。
