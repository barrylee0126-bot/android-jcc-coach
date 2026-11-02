# 本地候选资料库 v1

本模块用 Python 标准库读取调用方指定的本地 JSON，在内存中建立可查询的源记录和字段证据。它只提供候选图鉴，不裁决游戏规则，也不把来源声称的版本绑定为目标客户端当前补丁。

代码入口为 `src/jcc_catalog/`；Python 3.10 及以上，无新增依赖。运行时设置 `PYTHONPATH=src`。本规范和测试仅含合成数据；原始游戏资料、来源元数据、运行输入清单和查询结果由调用方保留在本地，不随模块再分发。

## 1. 输入与不变量

支持 `hero`、`trait`、`item`、`augment` 四类。每个输入默认是对象数组，也可用 `records_pointer` 指向 JSON 文档中的嵌套数组。数组中每个对象对应一条源记录；即使 ID、名称和全部内容相同，位置不同也保留为不同记录。

身份与作用域规则：

- `external_id` 只是源记录的字符串或整数 ID；`record_key` 才是本模块的记录引用。记录键由来源、种类、文件 SHA-256、来源上下文及原始 JSON 位置确定，不以供应方 ID 或中文名为唯一键。
- `snapshot_id` 包含资料文件摘要、来源 URL、可选采集时间、显式版本声明、元数据文件摘要及选取路径。相同输入重复装载不会重复计数，而是报 `duplicate_snapshot`；同一来源的不同快照可以并存。搬动同样的文件不改变键；改变内容或来源上下文会改变键，记录键不是跨版本稳定实体 ID。
- 游戏、地区、赛季、赛季主题、模式、补丁、热修复分别记录为 `claimed_scope`，缺项为 `null`。不从文件名、资源目录、ID、中文界面、客户端软件版本或补丁字符串的大小推断适用性。
- `source_url` 是调用方提供的未核验来源定位。记录中的 `source`、`verifiedAt`、`validation.valid` 等原样留在源数据里，始终不转为项目核验结果。
- 元数据文件的直接字段 `game / region / season / season_theme / mode / patch / hotfix` 可形成来源声明；`theme` 在没有 `season_theme` 字段时作为其来源声明。所有声明带原文件摘要和指针。显式 `claimed_scope` 另标为 `operator_supplied_source_claim`。两者不同则并列，查询投影为未知，列入 `scope_claim_conflicts`，不互相覆盖。

`loaded_at` 是本机本次读取时间。`retrieved_at` 是调用方可选提供的原始采集时间，要求带时区并标注 `operator_supplied`；未提供时为未知。供应方自己的时间字段留在元数据中，不自动填入这两个时间。文件修改时间不作采集时间。

不读取网页、不执行来源脚本、不访问设备、不修改来源、不写入数据库或缓存。所有公开方法返回副本，调用方修改查询结果不会改变内存中的源证据。默认每个资料、元数据或输入清单文件最大 32 MiB，总源记录上限 100,000，可在 Python API 中调整。

## 2. 输入清单

最小 CLI 可重复传 `--source KIND PATH`，其来源 URL、采集时间和版本声明保持未知。需要完整来源信息时，传本地输入清单 `--manifest PATH`；这两个入口互斥。清单里的相对路径以清单所在目录为基准。

下例全部为合成内容，可放在本地临时目录。`records.json`：

```json
[
  {"id": "demo-shared", "name": "示例", "cost": 1, "recipe": []},
  {"id": "demo-shared", "name": "示例+", "cost": 2, "recipe": ["部件A", "部件A"]}
]
```

`claims.json`：

```json
{"patch": "demo-patch", "season": "DEMO", "validation": {"valid": true}}
```

`input.local.json`：

```json
{
  "schema_version": 1,
  "sources": [
    {
      "source_id": "synthetic-example",
      "kind": "item",
      "path": "records.json",
      "records_pointer": "",
      "source_url": "https://example.invalid/reference",
      "retrieved_at": "2026-01-01T00:00:00Z",
      "claims_path": "claims.json",
      "claims_pointer": "",
      "claimed_scope": {"game": null, "region": null, "mode": null}
    }
  ]
}
```

`source_id / kind / path` 必填，其余可选。清单仅允许 `schema_version / sources`，每条来源只允许 `SourceSpec` 的字段；拼错字段、`verified` 等未经定义的核验开关均会失败。`claims_path` 可指向完整的本地供应方元数据；它不需要被复制进公开目录。`source_metadata(snapshot_id)` 可显式读取原样的选中对象副本，CLI 默认不打印该对象的全部内容。

```sh
PYTHONPATH=src python3 -B -m jcc_catalog --manifest /absolute/local/input.local.json summary
PYTHONPATH=src python3 -B -m jcc_catalog --manifest /absolute/local/input.local.json sources
PYTHONPATH=src python3 -B -m jcc_catalog --manifest /absolute/local/input.local.json find --kind item --id demo-shared
```

CLI 先列出每条候选的 `record_key`、源位置、可用顶层字段、来源声明及核验边界，默认不输出原始完整记录。`find` 默认最多显示 20 条，支持 `--limit 1..1000 / --offset`，`total` 保留匹配总数。`--id` 匹配字符串 ID；整数 ID 用 Python API 查询。

```sh
PYTHONPATH=src python3 -B -m jcc_catalog --manifest /absolute/local/input.local.json record RECORD_KEY
PYTHONPATH=src python3 -B -m jcc_catalog --manifest /absolute/local/input.local.json field RECORD_KEY /cost
PYTHONPATH=src python3 -B -m jcc_catalog --manifest /absolute/local/input.local.json find --claimed patch demo-patch
```

将 `RECORD_KEY` 换为前次查询的完整记录键。`--claimed` 精确筛选供应方声明，不证明当前客户端适用；Python API 中以 `None` 筛选未知维度，未知不作为任意版本的通配符。

## 3. Python API

```python
from jcc_catalog import Catalog, CatalogError, FieldRef, SourceSpec, load_manifest

catalog = Catalog(load_manifest("/absolute/local/input.local.json"))
records = catalog.find(kind="item", external_id="demo-shared")
first = catalog.field(records[0]["record_key"], "/cost")

# 默认差异保留为变体或作用域待判，不认定同 ID 就是同一事实。
comparison = catalog.compare_fields([
    FieldRef(record["record_key"], "/cost") for record in records
])
```

| 接口 | 返回与用途 |
| --- | --- |
| `Catalog(sources, max_file_bytes=..., max_records=...)` | 从 `SourceSpec` 可迭代对象读取；任一坏输入导致构建失败，不跳过记录 |
| `load_manifest(path)` | 读取并校验本地 v1 输入清单，返回 `SourceSpec` 列表 |
| `sources()` | 来源 URL、快照路径/摘要/时间、元数据定位、来源版本声明和核验边界 |
| `source_metadata(snapshot_id)` | 显式读取原样的供应方元数据对象副本，不是项目验证 |
| `find(kind, name, external_id, text, source_id, claimed_scope)` | 可组合精确筛选；`text` 仅搜索源名称和别名子串；不按名称合并实体 |
| `record(record_key)` | 记录身份、源位置、可用字段、作用域声明和候选状态 |
| `raw_record(record_key)` | 显式取出原始源对象副本，不解析或改写游戏文本 |
| `field(record_key, pointer)` | 字段候选值、快照摘要和完整 RFC 6901 指针；裁决值恒为空 |
| `compare_fields(refs, relationship="unresolved", reason=None)` | 并列字段证据；可显式声明比较假设，返回候选、未知或原值差异冲突 |
| `require_verified_value(record_key, pointer)` | v1 一律报 `unverified_field`，阻止把候选直接当决策值 |
| `summary()` | 各类记录数、每快照重复 ID 组数及额外记录数、未知身份计数、决策可用数等 |

字段指针相对记录对象。数组索引及名称中 `/`、`~` 的转义遵循 RFC 6901，例如 `/stats/0/hp`、`/a~1b/~0x`。根指针 `""` 显式请求整条记录；不提供通用公开数据导出命令。

## 4. 字段证据与未知

`field()` 示例形状如下，值为合成数据，摘要和定位只作占位展示：

```json
{
  "status": "candidate",
  "value": null,
  "candidates": [{
    "assertion_id": "assertion-<digest>",
    "record_key": "record-<digest>",
    "field_pointer": "/cost",
    "source_value": 1,
    "value_state": "present",
    "status": "candidate",
    "origin": {
      "snapshot_id": "snapshot-<digest>",
      "source_id": "synthetic-example",
      "source_url": "https://example.invalid/reference",
      "local_path": "/absolute/local/records.json",
      "sha256": "<file digest>",
      "record_pointer": "/0",
      "json_pointer": "/0/cost",
      "locator_exists": true
    },
    "source_claimed_scope": {"patch": "demo-patch"},
    "conditions": "unknown",
    "unit": null,
    "semantic_interpretation": "not_performed",
    "verification": {
      "evidence": "third_party_only",
      "project_review": "not_reviewed",
      "version_applicability": "unknown",
      "target_client_binding": "unknown",
      "decision_eligible": false
    }
  }],
  "reason": "no_independent_field_verification",
  "decision_eligible": false
}
```

示例省略了 `loaded_at / retrieved_at / size_bytes / scope_evidence / scope_claim_conflicts` 等实际返回字段，不代表可填造它们。所有实际来源维度均返回，包括未知维度。

| 源内容 | `value_state` | 字段状态 | 保留的含义与限制 |
| --- | --- | --- | --- |
| 路径不存在 | `missing` | `unknown` | `locator_exists=false`，不是来源明确否定 |
| 显式 `null` | `null` | `unknown` | 来源提供了空值，不补写 |
| 空/纯空白文本 | `empty_text` | `unknown` | 原始文本仍保留 |
| `0`、`false` | `present` | `candidate` | 是原始候选值，不当作缺失；不解释布尔字段的实际规则语义 |
| 空数组/对象 | `empty_container` | `candidate` | 来源确实写了空容器；规则含义未核验，不推断“无配方”“无加成”或不适用 |
| 其它非空值 | `present` | `candidate` | 文本、嵌套数组、重复配方组件及模板原样保留，不解析系数或补全条件 |

外层 `value` 恒为 `null`；只有 `candidates[].source_value` 带来源原值。即使多个来源相同，`decision_eligible` 仍为 `false`，无冲突不等于经核实。v1 不提供把 `source_value` 直接提升成 `verified` 的参数。

## 5. 冲突表示

`compare_fields` 只比较调用方选择的原始字段，不扫描文本推断参数，不以 ID 或名称自动建立等价关系。

- 默认 `relationship="unresolved"`。不同原值返回 `candidate / variant_or_scope_difference`，保留变体、条件或历史版本差异的可能性。
- 只有调用方显式指定 `relationship="same_fact"` 并给出 `reason`，才把相同或未知声明范围下的原值差异表示为 `conflict / raw_value_disagreement`。这是可审查的比较假设，不是本模块核实了语义矛盾；`semantic_contradiction="not_determined"`、`project_resolution="unresolved"`。
- 已声明为不同游戏、地区、赛季、模式、补丁或热修复的候选，返回 `candidate / different_claimed_scope`，不在跨范围数据中选胜者。声明有缺项时 `scope_status="unknown"`。
- 原值全部相同时仍为 `candidate`；缺失值使 `completeness="incomplete"`，不会把缺失值当作反对证据。全部缺失/空值时返回 `unknown`。
- 比较不决定不同站点是否独立。完全相同的字段引用重复传入会报错；转载、同一上游的不同快照仍可能相关，不计投票，不自动提高置信。

```sh
PYTHONPATH=src python3 -B -m jcc_catalog --manifest /absolute/local/input.local.json compare \
  --field RECORD_KEY_A /cost --field RECORD_KEY_B /cost \
  --relationship same_fact --reason '合成案例：人工声明这两个字段应表述同一事实'
```

原文措辞、条件、单位不同也可能产生原值差异，须由后续字段断言和核验流程判断；不能把这个结果直接当成两个技能参数不同的证明。v1 不导入公告作为完整基础规则，也不据公告修补原始资料。

## 6. 错误与可复现检查

错误抛 `CatalogError`，带稳定 `code`。CLI 对输入/查询错误在标准错误输出 `{"error":{"code":...,"message":...}}`，退出码 2；成功在标准输出返回 JSON，退出码 0。参数语法错误沿用 `argparse` 帮助和退出码 2。错误消息不回显来源原文或字段值。

检查包括非 UTF-8、坏 JSON、重复 JSON 对象键、NaN/Infinity/数值溢出、错误数组形状、混入非对象记录、无效指针、缺失文件、未知记录键、无时区采集时间、重复快照及输入大小上限。发现重复对象键时拒绝整个输入，避免标准 JSON 解码器悄悄丢失旧值；数组里的重复记录则完整保留。

运行全部合成测试：

```sh
PYTHONPATH=src python3 -B -m unittest discover -s src/jcc_catalog/tests -v
```

测试使用临时目录，自行生成并清理合成资料；覆盖相同 ID 和完全相同变体保留、未知来源/版本、声明冲突、源定位/摘要、嵌套字段、未知/空值、配方重复组件、类型差异、跨作用域比较、候选一致仍不核验、决策阻断、返回副本隔离、坏输入和 CLI。没有原始游戏数据、联网或设备依赖。

## 7. 与局面和后续核验的衔接

本模块不依赖局面契约的最终字段。识别层可以用观测到的名称或 ID 请求候选，保留零条/多条结果以及 `record_key / snapshot_id / assertion_id`。这只是文字到候选图鉴的联系，不证明识别正确、实体身份确定或规则适用。

后续适配层应把局面来源帧和观察时间留在局面侧，把资料摘要和字段证据留在资料侧。局面的 `recognized / unknown / conflict / stale` 与本模块的 `candidate / unknown / conflict` 不能直接互换；资料没有画面新鲜度，来源声明也不能反填为局面中已确认的游戏版本。

版本核验交付后，可另外定义带精确作用域、单位、条件、人工读取定位和审查记录的字段断言/裁决层，引用本模块键和摘要，不改源记录。公告只核实局部变化时，其它字段仍保留候选；只有完整的版本适用性与所需输入证据明确后，策略层才能消费裁决结果。v1 不实现该裁决层、不接入策略、也不宣称覆盖全部英雄/强化或当前国服规则。
