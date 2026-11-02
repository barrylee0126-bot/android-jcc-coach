# 候选资料与版本证据衔接 v1

代码入口：`src/jcc_catalog/evidence.py`。Python 3.10 及以上，仅使用标准库。通过 `from jcc_catalog.evidence import ...` 使用；现有 `jcc_catalog` 包入口、CLI 与 [候选资料库 v1](catalog-v1.md) 保持各自的接口。

本层建立“候选记录 / 字段 ↔ 字段断言 ↔ 来源证据”的显式关联，保留语义、单位、条件、作用域、时间、比较假设与修订记录。适合追溯历史公告、整理固定案例依赖，以及回答当前还缺什么依据。

**v1 不提供当前实机决策值。** `explain()` 的 `value` 恒为 `null`、`decision_eligible` 恒为 `false`；`require_current_value()` 一律抛 `current_value_unavailable`。即使所有输入看起来齐全，仍不能放行。现有 `Catalog.require_verified_value()` 的阻断行为不变。

## 1. 信任边界

- `official_documented / client_observed / third_party_only / unknown` 是调用方记录的证据性质，输出明确标记 `classification_origin=caller_supplied`。本模块不访问网址、验证发布者身份或重读图片。
- `reviewed_at + review_note` 表示已有一条审阅记录，来源输出 `authenticity=review_recorded`；未填时为 `unknown`。`authenticity_authenticated` 恒为 `false`。历史公告身份的审阅记录与当前客户端适用性分开保存。
- 客户端绑定和后续修订覆盖同样是带引用的输入记录，输出 `authority=caller_report_only`。这些记录不能自行签发信任。此版本没有可信审阅者、设备验证器或字段裁决器。
- 所有数据类都没有 `verified` 参数，传 `verified=True` 会产生 `TypeError`；定位和作用域中的未定义键也会失败。候选原文或 `asserted_value` 内部出现 `verified` 等字样只作为 JSON 内容保留。
- 不以相同来源 ID、外部编号、名称、补丁标签、数量一致或多个值相同来合并实体、投票或选择胜者。源记录只通过现有 `record_key + FieldRef.pointer` 引用；不同变体继续分别存在。

此边界是接口契约，不是抵御恶意 Python 调用者修改进程内部对象的安全隔离。后续若需要放行当前实机值，应另行定义可信证据输入、完整性检查和逐字段裁决协议，不能将这里的输入标签解释成已完成的核验。

## 2. 记录结构

所有数据类以不可赋值的数据记录提供，加入索引时再作校验和深拷贝；调用方随后修改输入里的嵌套字典或返回结果不会改写索引。记录通过 `add_*()` 逐条加入，同一类别的 ID 重复会报错，不覆盖旧记录；不同类别之间没有隐式 ID 连接。

| 记录 | 必填字段 | 其他主要字段与含义 |
| --- | --- | --- |
| `SourceEvidence` | `evidence_id, evidence_kind, locator` | `scope, read_method`；`source_published_at, retrieved_at, observed_at, reviewed_at, review_note` |
| `FieldAssertion` | `assertion_id, evidence_id, subject_key, field_key, asserted_value` | `variant_key, semantic, unit, conditions, interpretation_note, scope, release_event_id, announced_effective_at, observed_effective_at, valid_until` |
| `RevisionEvent` | `revision_id, evidence_id, assertion_ids, reason` | `scope, effective_at, replacement_ids`；准确引用被影响断言，不按名称猜测影响范围 |
| `RevisionCoverage` | `coverage_id, evidence_id, assertion_ids, checked_from, checked_through, method` | `scope`；某些具体断言在有限区间内的覆盖报告，非整个游戏的全局核验开关 |
| `ClientBinding` | `context_id` | `scope, evidence_ids, release_event_ids, observed_at, expires_at`；不要求写真实设备标识，允许部分未知 |

字段关联由 `link_field()` 生成 `link_id`，记录具体 `record_key, field_pointer, assertion_id, relationship, reason`。比较记录由 `record_comparison()` 保存两条断言、比较假设、理由与结果。没有自动修改原始图鉴、文本解析、单位换算、实体归并或冲突裁决。

### 2.1 定位与时间

`locator` 仅允许以下键，未填者输出 `null`：

| 键 | 含义 |
| --- | --- |
| `url` | HTTP(S) 具体页面或图片地址，不允许用户名和密码 |
| `local_path` | 本地快照或核验记录位置；不会由本模块主动读取 |
| `sha256` | 调用方登记的 64 位小写十六进制摘要；只验证格式，不冒充本轮重算 |
| `json_pointer` | 快照内 RFC 6901 定位；本层不验证此快照内容是否确实存在 |
| `section` | 标题、图号、行、区域或其他人工读取位置 |
| `observation_id` | 客户端观察或来源帧引用，只允许 `client_observed` 来源 |

至少提供 `url / local_path / observation_id` 之一。字段关联的候选侧定位由 `Catalog.field()` 实际解析，保留快照摘要、完整 JSON Pointer、采集与加载时间、字段存在状态和原始值；它与调用方登记的公告定位有不同的验证边界。

时间均可未知；已填写的时间必须是带时区的 ISO 时间戳，按真实时间比较，保留调用方原来的字符串。仅有日期或分钟级记录时不要补造精确秒数，可在 `review_note / interpretation_note` 说明原始精度和时区解释，无法表达为精确时间的字段保持 `null`。

文档发布日期、抓取时间、审阅时间、计划生效时间、客户端观察时间、观察到的生效时间分别存储。`observed_at` 只允许用于 `client_observed` 来源；`observed_effective_at` 还要求该来源确有 `observed_at`。公告计划时间不能直接写成客户端观察事实。时间值本身仍是输入声明，本层不会证明某次观察确实捕获了生效起点。

`read_method` 可另记人工读图、正文读取、OCR、包信息查询或承接既有核验记录等方法；未提供时保持未知。方法名称不改变来源或客户端适用性的信任等级。

### 2.2 语义、变体和作用域

`subject_key` 是调用方明确分配的主体候选键，`variant_key` 单独保存；二者不是官方实体鉴定结果。未核变体使用 `null`，不能用相同编号或名称补成基础版。`field_key` 是非空 RFC 6901 语义字段路径，例如 `/reward/gold`，与候选原始字段路径分开。

`asserted_value` 保存某来源所表述或调用方读取出的候选值，外层当前决策 `value` 仍为空。`semantic / unit / conditions / interpretation_note` 限定这次读取的含义。未知条件用 `null`；空条件对象 `{}` 是调用方明确声称没有附加条件，模块不会替其证明。百分数、百分比点、倍率、属性加成与绝对伤害不会互相换算，局部参数也不会提升整条技能或对象。

作用域沿用资料库七轴：`game, region, season, season_theme, mode, patch, hotfix`，均为非空字符串或 `null`。输入可省略，输出补齐未知轴。来源、断言、修订、覆盖报告与客户端各自保存作用域，不继承填空；一份公告中的不同内容可登记为多条不同作用域断言。

`season_id / balance_patch` 等其他契约字段需由调用方显式映射为 `season / patch`，本层拒绝未知键，也不从 App 构建号、资源号、URL、文件名或补丁字符串推导。追加事件另由 `RevisionEvent` 登记；`hotfix=null` 不能解释为已确认没有热修复。

作用域比较逐轴返回 `same / different / unknown` 及不同或未知的维度。只有七轴都已填写且相同才为 `same`；任何已知不同优先为 `different`。`null` 不作任意地区、模式或版本的通配符。TFT 官方公告与其他游戏的相同补丁号不会建立迁移关系。

## 3. 接口

运行时设置 `PYTHONPATH=src`。公开查询返回深拷贝，结果均可 JSON 序列化。

| 接口 | 用途 |
| --- | --- |
| `EvidenceIndex(catalog)` | 绑定一个既有候选资料库，在内存建立独立证据图 |
| `add_source(SourceEvidence)` / `source(evidence_id)` | 登记、读取来源性质、位置、时间、审阅记录和来源范围 |
| `add_assertion(FieldAssertion)` / `assertion(assertion_id)` | 登记、读取逐字段断言；必须引用已存在的来源 |
| `find_assertions(subject_key=None, variant_key=None, field_key=None, release_event_id=None, scope=None)` | 精确筛选；省略的条件不筛选，`scope={"patch": None}` 只匹配未知补丁；`variant_key=None` 表示不筛变体 |
| `link_field(FieldRef, assertion_id, reason=..., relationship="unresolved")` | 显式关联候选字段与断言；`reason` 必填；相同关联不能重复登记 |
| `trace_field(FieldRef)` | 从字段返回原始候选及所有直接关联断言、来源、范围对照、原值关系；不扩展到同 ID 或同名记录 |
| `compare_assertions(left_id, right_id, relationship="unresolved", reason=None)` | 对照两条断言的主体、变体、语义、单位、条件、范围和类型保真的原值 |
| `record_comparison(comparison_id, left_id, right_id, reason=..., relationship="same_fact")` | 保存显式比较假设，供 `explain()` 返回冲突或待核状态；不选择采用值 |
| `add_revision(RevisionEvent)` | 登记有精确目标、来源和作用域的修订线索；可引用替代断言但不自动采用 |
| `add_coverage(RevisionCoverage)` | 登记具体断言的有限区间覆盖报告；不自动拼接多个部分区间 |
| `explain(assertion_id, at=..., client=None)` | 返回来源、断言、候选反向引用、显式比较、范围对照、修订、覆盖、客户端输入及阻断原因 |
| `require_current_value(assertion_id, at=..., client=None)` | 一律抛 `EvidenceError(code="current_value_unavailable")`，错误的 `reasons` 元组保留阻断代码 |

`EvidenceError` 继承现有 `CatalogError`，带稳定 `code`；候选侧引用错误仍可能抛原 `CatalogError`。错误消息不包含原始字段值。未知来源 / 断言、悬空或重复引用、重复记录 ID、非法时间、颠倒区间、非有限 JSON 值、错误定位和作用域均会失败；本层不吞掉坏输入继续导入。

字段关联的 `relationship` 只接受 `unresolved / same_fact`，两者均为调用方假设。`trace_field()` 的 `raw_value_relation=equal / different / unknown` 只比较原始 JSON 值，始终伴随 `semantic_consistency=not_determined`。把公告中的一个数字关联到整段图鉴文本，并不证明两段文字具有同样的条件和含义。

只有显式 `same_fact` 且理由非空、主体与已知变体相同、语义 / 单位 / 条件相同、两个断言及其来源范围都明确相同时，原值差异才输出 `status=conflict, comparison=raw_value_disagreement`。这是带审查假设的差异，不是语义矛盾的自动证明，`semantic_contradiction=not_determined`、`resolution=unresolved`。缺项或不同作用域会单列，原始差异仍保留。相同值也不标 verified，不计来源票数。

## 4. 时间、修订与当前适用性诊断

`at` 必须显式填写，表示本次判断使用的时间截点，便于回放。不会自动取机器现在时间。已知的发布、抓取、观察或审阅时间晚于 `at` 时，该来源标为未来证据；显式比较仍可查看原始结果，但 `availability=evidence_from_future`，不会把未来反证倒写成截点时已知的冲突。未知时间不会假装已通过时间检查。

断言的有效起点优先使用已记录的 `observed_effective_at`，否则使用 `announced_effective_at`，并返回 `start_origin`。这只是诊断所用的声明时间：`effective_in_client` 仍为 `not_established`。状态包括：

- `not_yet_effective`：尚未到所登记的起点，包括公告已经发布但计划将来生效。
- `start_unknown / end_unknown`：缺少起点或明确终点；未知终点不会变成无限有效。
- `inside_declared_window / expired`：显式 `[start, valid_until)` 的内部或已过终点；终点恰好到达即过期。

修订只作用于显式 `assertion_ids`。跨游戏或不同已知范围为 `different_scope`；未来来源为 `evidence_from_future`；未到生效点为 `scheduled`；生效点到达后为 `due_unadjudicated` 或 `due_scope_unknown`。时间未知为 `effective_time_unknown`。`replacement_ids` 仅提供可追溯联系，绝不覆盖旧值或选出新值；新修订也不提升其它字段。

覆盖报告须有来源、方法、精确目标与有限的闭区间 `[checked_from, checked_through]`。起点晚于断言生效起点、区间已过期、范围不明或不同、未来来源都不能报告覆盖。`checked_through > at` 标为 `check_boundary_from_future`，避免拿一次未来完成的检查回填更早的判断；`at` 超过 `checked_through` 的任意时长均标 `outdated`，本版不设未经实测的宽限时间。因此保守的 `reported_interval_covers_query` 通常只出现在明确检查边界上，仍只表示记录的区间关系，伴随 `revision_coverage_not_authenticated`，不表示本模块证明了后续事件穷尽性。多条部分覆盖不会自动拼成完整链。

客户端输入分别检查来源是否是带观察时间的 `client_observed`、范围是否对得上、事件是否有明确绑定、时间是否来自未来，以及 `expires_at` 是否未知或已到。过期边界同样排除终点。客户端来源的完整性或标签不能证明 App 版本已映射到游戏平衡补丁，且最终始终有 `client_binding_not_authenticated`。

`explain()` 会同时返回所有有关缺项，包括已记录冲突和到期修订。即使这些形状检查均满足，仍返回 `current_value_adjudication_not_implemented`；本层没有当前值放行路径，也不能用来源有官方标签替代设备绑定、后续修订完整性、字段含义或必要条件的核验。

## 5. 可运行的虚构示例

下例只在临时目录创建两条虚构变体，来源网址使用 `example.invalid`。所有主体、数值、条件、版本和时间均为合成内容；没有游戏截图、真实图鉴或设备标识。

```python
import hashlib
import json
import tempfile
from pathlib import Path

from jcc_catalog import Catalog, FieldRef, SourceSpec
from jcc_catalog.evidence import EvidenceIndex, FieldAssertion, SourceEvidence

scope = {
    "game": "demo-game", "region": "demo-region", "season": "DEMO",
    "season_theme": "示例主题", "mode": "demo-mode",
    "patch": "demo-patch", "hotfix": "demo-base-event",
}

with tempfile.TemporaryDirectory() as temporary:
    path = Path(temporary) / "synthetic.json"
    path.write_text(json.dumps([
        {"id": "shared", "name": "示例", "gold": 4},
        {"id": "shared", "name": "示例+", "gold": 8},
    ], ensure_ascii=False), encoding="utf-8")
    catalog = Catalog([SourceSpec("demo-candidates", "augment", path, claimed_scope=scope)])
    records = catalog.find(external_id="shared")  # 保留两条源记录
    ref = FieldRef(records[0]["record_key"], "/gold")
    index = EvidenceIndex(catalog)
    index.add_source(SourceEvidence(
        "candidate-snapshot", "third_party_only",
        {"local_path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
         "json_pointer": "/0/gold"},
        scope=scope, retrieved_at="2026-01-01T00:00:00Z",
    ))
    index.add_source(SourceEvidence(
        "notice", "official_documented",
        {"url": "https://example.invalid/demo-notice/image-2", "section": "图2：奖励行"},
        scope=scope, source_published_at="2026-01-01T00:00:00Z",
        retrieved_at="2026-01-01T00:00:00Z", reviewed_at="2026-01-01T00:00:00Z",
        review_note="虚构的发布者身份和参数审阅记录",
        read_method="demo-human-reading-record",
    ))
    for assertion_id, evidence_id, value in (
        ("candidate-reading", "candidate-snapshot", 4),
        ("notice-reading", "notice", 6),
    ):
        index.add_assertion(FieldAssertion(
            assertion_id, evidence_id, "demo-subject", "/reward/gold", value,
            scope=scope, variant_key="base", semantic="initial_gold_reward", unit="gold",
            conditions={"round": "demo-3-2"}, release_event_id="demo-release",
            announced_effective_at="2026-01-01T01:00:00Z",
        ))
        index.link_field(ref, assertion_id, reason="虚构案例：指定基础变体的奖励字段")
    index.record_comparison(
        "demo-conflict", "candidate-reading", "notice-reading",
        reason="虚构案例：人工明确两次读取应表述同一条件下的奖励",
    )
    result = index.explain("notice-reading", at="2026-01-01T02:00:00Z")
    assert len(records) == 2
    assert index.trace_field(FieldRef(records[1]["record_key"], "/gold"))["links"] == []
    assert result["assertion"]["asserted_value"] == 6
    assert result["comparisons"][0]["assessment"]["status"] == "conflict"
    assert result["candidate_links"][0]["field"]["candidates"][0]["source_value"] == 4
    assert result["value"] is None and result["decision_eligible"] is False
```

该例能说明“虚构公告记录为 6，源候选仍是 4，当前值尚不可用”。不会据同 ID 把第二条变体也改为 6。完整的修订、覆盖和客户端输入示例见本模块合成测试；不应将合成记录作为真实放行依据。

## 6. 验证和局限

```sh
PYTHONPATH=src python3 -B -m unittest discover -s src/jcc_catalog/tests -v
```

测试在临时目录生成并清理合成资料，不联网、不操作设备。覆盖同编号 / 同名称变体、记录与字段定位、未知与不同作用域、公告与观察区别、条件和单位、显式冲突、类型保真、未来证据、计划修订、覆盖缺口、过期边界、假 verified、悬空引用与副本隔离，并继续运行原候选库测试。

本层没有持久化格式、数据库、批量公告解析、自动来源真实性检查、完整事件集构建、经济与概率表、客户端补丁映射或策略执行器。输入尚未核实的内容继续以声明和未知存在。当前可交付能力是可查询的证据关系与保守诊断，合成测试不证明真实资料正确、设备适用性或对局效果。

涉及原始画面、个人资料、第三方整包、权利未明素材及其本地快照的位置只应由调用方在本地管理；本规范和测试仅含虚构数据。记录一个可访问的 URL 或摘要不代表取得再分发许可。
