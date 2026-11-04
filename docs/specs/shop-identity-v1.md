# 商店文字与资料名称的离线候选匹配 v1

`jcc_shop_identity` 把现有 `jcc_shop_probe` 的商店显示文字结果，与 `jcc_catalog_snapshot` 接受的一份调用方本地资料请求连接起来。它只返回该份资料中的**名称匹配候选**，不确认棋子身份，不生成购买、出售、上阵或其他建议。它不联网、不读取游戏素材、不存储请求或输出、不操作设备；当前没有接入实时观察、播放器或提示界面。

## 直接复用和调研

- 文字来自 `ShopDisplayTextProbeResult.summary()`；其 1×、2× 检查仍是机器候选，名字区域也可能包含特殊商品，不能被当成棋子真值。
- 输入资料沿用 `jcc_catalog_snapshot.analyze_request`，重新验证完整请求并复用其 `name_lookup.heroes`，不信任调用方传入的已加工输出，也不复制一套资料解析器。此适配器要求来源、地区、模式、赛季和版本等为明确的**调用方声明**，并非来源或当前客户端认证。来源或资料版本未明时它返回 unknown，不能把占位数据填为已知绕过。
- 已有 GitHub 调研中，[TanzWen/jcc-tft-simulator 的固定 collector.py](https://github.com/TanzWen/jcc-tft-simulator/blob/a9cec17422e1742d67d09fdba7b51ce4be165c68/src/jcc/collector.py) 将游戏资料按模式、赛季、版本分开处理，其 [MIT 许可证](https://github.com/TanzWen/jcc-tft-simulator/blob/a9cec17422e1742d67d09fdba7b51ce4be165c68/LICENSE) 和项目已有适配器规格均保留；本模块未复制其代码或游戏数据。另查阅了 [tft-scout](https://github.com/linkeLi0421/tft-scout) 和 [TFT-OCR-BOT](https://github.com/jfd02/TFT-OCR-BOT) 的 OCR 商店功能说明；它们面向 TFT，未引入其代码、模型或数据。项目此前两个上游引用仍见根目录第三方说明。

## 请求与边界

`resolve(request)` 接收一个 Python 字典：

| 字段 | 要求 |
| --- | --- |
| `format_version` | `jcc-shop-identity-request/v1` |
| `display_text` | 上述商店探针的完整 `summary()`；探针是未认证来源的离线输出。 |
| `sanitization_evidence` | 同一次探针结果的 `sanitization_evidence_v1()` 包装。字段可缺省以兼容原有请求，但缺省时所有槽位均为 unknown。 |
| `catalog_request` | 已发布 `jcc_catalog_snapshot` 的完整输入请求，不是预适配器结果或经过调用方修改的适配器输出。 |
| `catalog_namespace` | 调用 `catalog_namespace(catalog_request)` 计算，形如 `sha256:…`；与实际请求不符时拒绝。它绑定的是完整请求的规范 JSON，不是来源文件本身的摘要，也不认证来源。 |
| `catalog_context` | 精确包含 `coverage` 和 `conflicts`。覆盖必须是 `declared_complete`；冲突为 `none_declared` 或下述同一快照的已定位冲突清单。它们都是调用方声明，模块不能证明全量或发现未提供的冲突。 |
| `frame_context` | 精确包含 `frame_ref`、`scope`、`freshness` 和 `sample_kind`，帧引用须与探针相同。`scope` 是 `current_frame` 或 `historical_frame`；时效为 `fresh`、`stale` 或 `unknown`；样本类型为 `synthetic`、`replay` 或 `unknown`。当前画面只有调用方声明 `fresh` 才继续匹配；显式标注的历史帧不受当前画面时效限制，只能对这一帧做离线分析。`replay` 不能同时声称当前画面；类型、时效或范围缺失、格式异常均不匹配。 |

单个请求只处理一份资料：候选由 `{catalog_namespace, hero_key}` 一起标识。相同键出现在不同资料请求时命名空间不同，不自动合并；对内容或声称来源做细微修改也会改变命名空间。不能把两个来源或赛季的 ID 同名当作同一棋子。散列只绑定输入内容，不能证明来源、覆盖、对局时间、认证或版本适用。候选引用和散列可能关联本地资料，只应保存在调用方私有环境，不应提交私人原始资料。

25C 本身不接受冲突字段。桥接层允许另传 `conflicts: {"status": "declared_scoped", "catalog_namespace": "...", "records": [...]}`，其中每个条目精确包含 `entity_kind: hero/trait/item`、同一 25C 请求中存在的 `key` 和 `field_scope: "not_display_name_or_alias"`。清单非空、最多 256 条，命名空间必须相符。英雄冲突会排除该英雄，羁绊冲突会排除引用它的英雄；与棋子名称无关的装备冲突不影响名称查找。同名统计先于排除：即使其中一条英雄被排除，也不能把其余同名项误报为唯一候选。若冲突可能涉及名称或别名、目标不存在、清单不完整或关联不明，调用方必须传 `unknown`，全部保留 unknown；桥接层不能自行验证调用方标记或遗漏。此机制不修改 25C，也不会将不能满足 25C 来源、版本及结构的资料转为可用输入。

对于商店槽位，必须有两个 1×、2× 候选视图且均无原因码、原始 OCR 分数至少 0.5，文字字段本身必须符合现有探针的 NFKC 与去空白规则。另须有独立的 `shop-display-text-sanitization-evidence/v1`：包装的格式、用途、策略及 `frame_ref/source_sha256/layout_id` 必须与显示摘要相符，完整摘要摘要必须由公开函数重算相等，五个槽位各出现一次；每个槽位摘要必须由公开函数重算相等，且该槽两个不同倍率均为 `raw_text_safety: safe`、`contains_control_or_format: false`。缺失、冲突、未知、假值类型、槽位或摘要错配都不能形成相应候选；全局包装无效时五槽均 unknown。实际探针在归一化前检查原文是否包含 `Cc/Cf/Cs`，不传出或保存原文；普通空格沿用原规则。

分数是引擎原始量，不代表准确率或经过校准的概率。安全包装仅是上游或调用方的声明；完整摘要哈希覆盖显示摘要，槽位哈希覆盖该摘要哈希及槽号，**均不覆盖包装内的倍率安全状态本身**。修改或复制这些状态可能不改变绑定哈希，因此这些检查不能防止所有安全状态误置，也不能证明包装真的由探针生成。更不能据此确认来源、真实身份或决策资格。格式错误、低分、未识别、全局探针问题、槽位重复或缺失均返回 unknown；桥接无法证明视图属于同一真实画面，或者平板正在显示该画面。

资料名称使用探针相同的 NFKC 和空白规范化，再按**完整名称**查找；同名或规范化后重合的不同键一律 unknown，并给出匹配数量，不选任一键。不做繁简转换、模糊纠错、别名扩展、大小写折叠或子串匹配。名称含控制、代理或不可见格式字符，或规范化后为空时拒绝；任一资料名称无效则整份不匹配，避免未被索引的名称掩盖歧义。

每个槽输出 `catalog_match_status: candidate/unknown`、可空的 `candidate_ref`、`match_count`、`identity_status: unknown` 和原因。输出分别保留 `frame_scope`、调用方声明的帧 `frame_sample_kind` 和资料 `catalog_data_kind`；两者独立，类型均未认证。`historical_frame` 只表示绑定这一帧摘要的离线匹配候选，不是当前局面或当前对局建议。即使唯一匹配，仍固定 `identity_status: unknown`、`target_client_binding: unknown`、`source_version_applicability: unknown`、`decision_eligible: false`、`action_advice: null`，这条候选不能用作行动或玩家提示。真实来源、实际版本和名称区域是否确实是棋子，需独立证据。帧引用相同和 `freshness: fresh` 均只检查调用方数据，不能证明来自原录像或当前屏幕；本无状态函数没有计时器或主动撤回功能，消费方须在状态更新、超时或断流时撤销并重新评估，不能继续展示旧结果。

## 试验及验证

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest discover -s src/jcc_shop_identity/tests -v
```

虚构测试通过真实探针 API 和已发布资料 API 组合，覆盖唯一名称、同名、规范化冲突、字符差异、来源版本未知、缺字段、已声明冲突、命名空间变动、低证据、失效以及畸形输入。测试不包含真实游戏数据，不提供准确率和实机成绩。历史开发帧或资料若不能满足版本、来源、时效、冲突及输入格式条件，保持 unknown，不能通过修改声明把它们升级。
