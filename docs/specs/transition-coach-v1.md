# 阵容转型候选比较 v1

代码入口：[jcc_transition_coach](../../src/jcc_transition_coach/)。它面向“玩家自己操作、系统给提示”的窄场景：比较调用方**已经用 jcc_team_planner 通过校验**的同版本局面，与一至三个调用方声明的目标阵容。输出重点是玩家能核对的事实：保留了哪些已知实例、还缺哪些棋子、当前商店能否补、已知即时购买成本、哪些棋子会离开棋盘、会开启或失去哪些已声明羁绊，以及哪些条件尚未满足。

它不会发现目标阵容，不预测未来来牌、刷新或卡池，不评估即时战力、胜率、装备效果或“最优转型”，也不输出出售、上阵、购买或点击指令。

## 共享事实合同

`compare(team_planner_request, target_rule_evidence)` 的第一个参数直接交给 [jcc_team_planner](../../src/jcc_team_planner/) 的 `plan()`。只有该规划器返回 `evaluated` 才继续比较。因此，棋子目录、羁绊门槛、人口、备战席、金币、棋盘、商店、准备阶段、六维版本绑定和来源声明完全复用既有合同，不建立第二套相互冲突的事实规则。

第二个参数 `target_rule_evidence` 是一条对应一个 `transition_targets.target_id` 的列表。每项必须恰含：

```json
{
  "target_id": "目标 ID",
  "binding": {"game":"…","region":"…","mode":"…","season":"…","patch":"…","rules_revision":"…"},
  "refs": ["同版本规则或攻略依据"]
}
```

其 `binding` 必须和共享局面完全相同，且每条路线各有不重复的非空引用。缺少路线依据、依据版本不一致或共享快照为 `unknown` 时整个结果为 `unknown`。引用仍是调用方声明：模块不认证攻略、OCR、录像或本局补丁绑定；未确认的自然之力/S18 混源资料不能仅改字段后成为已核实路线。

目标阵容直接复用 team planner 的 `transition_targets`：唯一 `target_id`、不重复的 `required_unit_ids` 和目录中有效的 `target_traits`。目标单位 ID 在本版本只能出现一次；同名多张、合星、纹章、特殊召唤物、装备效果与目标外填充都不推断。

## 输出与阻断

成功结果格式为 `jcc-transition-coach/v1`，`strategy_recommendation` 固定为布尔值 `false`，`comparison_scope` 固定为 `conditional_target_transition_comparison_only`，`execution_eligible` 固定 `false`。它们避免把一次结构比较误读为已可执行的策略建议。

局面证据和路线证据严格分层：顶层 `data_kind` 与 `snapshot_evidence` 只描述共享局面快照，`data_kind_scope` 固定为 `snapshot_only; route evidence separately caller_declared`；每条候选的 `target_rule_evidence` 单独记录路线绑定和引用，并固定 `verification: caller_declared_only`。无论字符串内容、`synthetic` 或 `verified` 声明如何，本模块不会将整条路线认证为真实有效。

每条候选包含：

| 字段 | 含义与边界 |
| --- | --- |
| `retained_instance_ids` | 每个目标单位保留一个已持有实例。顺序只优先避免扰动核心、已带装备、星级和已上场实例，**不是战力或携带者评分**。 |
| `current_board_to_bench_instance_ids` | 达成声明目标时会离开棋盘的已知实例；它不是出售指令。 |
| `core_instance_ids_not_in_target` | 目标遗漏核心的事实，阻断即时转型。 |
| `equipped_board_instance_ids_with_unknown_item_destination` | 已带装备棋子离场且装备去处未给出的事实，阻断即时转型。 |
| `visible_shop_matches_for_missing` | 目标缺口当前可见的一个商店格和价格。 |
| `missing_unit_ids_not_in_current_shop` | 当前商店未见的缺口。其目录价格不能当即时成本，`immediate_purchase_cost` 为 `null`。 |
| `known_current_shop_purchase_cost_for_missing_units_only` | 只有全部缺口当前可见时才有当前商店内缺口价格之和。`known_current_shop_purchase_cost_is_total_transition_cost` 固定 `false`，它绝不是总转型成本。 |
| `declared_base_trait_changes_from_current_board` | 只由调用方声明的基础棋子目录和门槛推导。`actual_trait_changes_from_current_board` 固定未知，因为纹章等修饰尚未建模。`declared_base_target_does_not_meet_its_trait_goal` 也只描述这个基础推算。两者都不能解释为战力变化或真实羁绊不满足。 |
| `operation_sequence_feasibility` | 固定 `unknown_not_modeled`。即便 `final_bench_capacity_sufficient_if_declared_moves_complete` 为真，模块也没有同版本的购入、上阵、下场和备战中间步骤模型，不能称即时可完成。 |
| `immediate_transition_feasible` | 固定 JSON `null`，表示未知而非 `false` 或可行动。未知原因由 `operation_sequence_feasibility` 表达；消费者不得将非空文本型 unknown 当布尔值使用。 |
| `conditions_or_blocks` | 目标超人口、目标空位、核心冲突、已装备棋子离场、备战席不足、商店外缺口、金币不足、基础目录推算的目标羁绊不满足和未建模操作序列。所有候选均为条件比较。 |

目标不满人口时，`target_open_slots` 保留为明确未知，模块不猜测过渡棋子。未来商店出现概率、来牌时间、装备转移、真实羁绊修饰、具体操作序列与转型收益固定为未评估。

## 与 25B、25C、25E 的接入

25B 视觉侧必须先给出同帧、未过期且足以组成 team planner 请求的完整字段；任何棋子、商店、人口、金币、羁绊或版本字段未知，就不能进入本模块。25C 给每条目标路线准备同版本来源映射；资料只支持条件原则时，应把条件写在目标选择外层，而不是假装为本局确定路线。25E 只在结果 `evaluated`、输入新鲜时展示“条件比较”卡，不能把它显示为立即执行卡；输入过期、断流、版本不匹配或结果未知时撤回旧提示。操作序列、真实羁绊或路线依据未核实时，界面必须保留对应未知。

## 运行和验证

仅依赖 Python 标准库及已发布的 `jcc_team_planner`，不联网、不写文件、不连接平板：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m jcc_transition_coach --demo
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s src/jcc_transition_coach/tests -v
```

`--print-synthetic-request` 输出纯虚构请求；`--stdin`、`--input FILE` 只读取显式 JSON；`evaluated` 返回 0，`unknown` 返回 2。`compare_json()` 会在解析时拒绝重复 JSON 键、NaN 和 Infinity。

测试覆盖当前可见缺口、商店外缺口、成本未知、羁绊变化、装备去处未知、核心冲突、目标空位、目标超人口、依据缺失或版本不符、共享局面未知、严格 JSON 与输入不变性。它们只验证接口边界，不代表真实版本、攻略有效性、识别准确率、端到端时延或上分结果。
