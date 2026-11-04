# 装备候选比较 v1

这个模块回答一个受限的问题：当调用方能给出**同一版本、完整已知的闲置散件库存**、当前棋子装备栏、成品到携带者的适配规则，以及可选的转型材料保留计划时，哪些当前可合成成品互相争用素材，它们各自有哪些尚有装备格的声明携带者，以及合成是否会破坏某个已声明的保留计划。

入口为 `jcc_equipment_coach.analyze_request` 或严格 JSON 入口 `analyze_json`。格式版本是 `jcc-equipment-coach-request/v1`。命令行只接受 `--demo`、`--input` 或 `--stdin`；`--demo` 全部使用虚构 ID 和资料。

## 前置条件与输出

顶层请求必须包含：

- `recipe_request`：原样传给已发布的 [jcc_recipe_candidate](recipe-candidate-v1.md)；它要求版本完全一致、库存 `complete: true`，且库存明确是 `unequipped_components`。画面中只看到部分散件、来源不是同一帧、或已装备/位置不明的散件，都不能写成空库存；
- `carrier_state`：同一 `binding`、同类证据下的可见棋子实例、已装备 ID 和明确的 `item_slot_capacity`；
- `carrier_fit`：同一版本、可追溯的成品到**实例**携带者规则。若无法提供，写 `{"status":"unknown","reason_zh":"..."}`，每个候选的携带结果都保持未知；调用方给出的适配规则不是装备强度或最佳归属证明；
- `reservations`：可选的同版本转型保留计划。每项都须有具体散件数量及条件说明；若目标阵容未确认，写未知，而不是把所有散件都宣布为必须保留。

所有已知块都要与 `recipe_request` 的 `game / region / mode / season / patch / rules_revision` 完全相同，并保持同一 `evidence.kind`。缺失、版本不一致、规则引用不存在的棋子、未完整库存或无效 JSON 都返回 `status: "unknown"`，不会悄悄补默认值。

成功时每一个 `candidates[]` 仍是“从同一份起始库存单独可合成”的候选。它新增：

- `carrier_evaluation`：`declared_available` 只表示某个调用方声明的携带者尚有格子；`declared_all_slots_full` 表示这些声明实例均满格；即使调用方称规则目录完整，但某成品没有适配条目，仍为 `unknown_missing_declared_rule`，不把“目录中没有”当成“游戏中没有”；没有携带者强度排序；
- `reservation_considerations`：一个已声明计划在合成前后分别缺哪些散件。合成后的每项短缺都给出该计划本身的需求数、实际合成后的剩余库存和缺口；`became_unreservable` 表示本次合成使原本能保留的计划变得不能保留，`worsened_existing_shortage` 表示计划本来已经缺料且本次又加重短缺，`uses_declared_reserved_components` 则明确本次确实消耗了计划所列散件。它们都不等于该计划更好；
- `material_conflicts`：对至多 64 个当前可合成的**不同成品候选**逐对检查；每条明确共同合成时的短缺散件。范围为 `material_conflict_scope: "evaluated_distinct_candidate_pairs_only"`，不检查同一成品做两件、三个以上选择共同合成或最大合成次数。超过 64 项时为 `null` 且范围为 `not_evaluated_candidate_limit`，不会用部分结果伪装成全量分析。

结果固定为 `strategy_recommendation: false` 和 `execution_eligible: false`。本模块不评价成品效果、棋子强度、羁绊收益、对位、掉落、刷新或胜率，也不进行合成、装备或平板操作。

## 与玩家提示的接入

25E 的提示层只应消费**同一个新鲜局面**中已成功评估的字段：成品 ID、两两素材冲突、声明携带者的剩余格子、以及条件化保留计划。卡片应将动作写成“可比较”而不是“最优”：例如“当前 A 与 B 不能同时合成；若仍满足已声明的转型条件，保留 X”。

提示层必须同时显示或保存本结果的 `binding`、证据引用和帧 ID/采集时间，并在新帧、库存/槽位未知、版本失配、候选过期或断流时撤回旧卡。结果固定给出 `frame_binding_verification: "not_verified_by_module"` 与 `freshness_verification: "not_verified_by_module"`：这个模块不读取帧，调用方的“同帧”“新鲜”声明不构成证明，更不能证明输入来自真实游戏或实际版本。

## 复用与验证边界

`jcc_recipe_candidate` 已固定复用 [ndh0408/tft-advisor](https://github.com/ndh0408/tft-advisor/tree/8380b6dbeffe9edade7b198d8063bc3e18917b6d) 提交 `8380b6dbeffe9edade7b198d8063bc3e18917b6d` 中 MIT 许可的 `craftableFrom` 函数片段。这个新模块没有复制游戏数据、图标、效果说明或强度模型；它仅消费该模块的明确候选并做素材计数、装备格和调用方规则的结构化比较。

项目内 S18 快照是混合第三方来源，且本机平衡补丁和热修未绑定，详见[版本资料说明](../../资料/版本数据/README.md)。模块不会自动加载这份快照，也不会把任何示例视为当前国服策略。

运行合成验证：

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  src/jcc_equipment_coach/tests/test_core.py -v
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -B -m jcc_equipment_coach --demo
```

测试只证明虚构完整输入下的契约：共用散件冲突、保留计划、装备格、缺规则、版本不一致、未完整库存、输入不变性和有界冲突枚举。它不证明真实识别准确率、版本适配、装备质量、提示时延或上分效果。
