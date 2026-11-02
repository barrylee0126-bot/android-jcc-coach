# 金币取舍条件分析 v1

状态：独立纯计算模块；尚未接入真实候选、正式策略或提示界面。日期：2026-11-02。

本模块回答一个窄问题：**如果候选金币为 G，单独花 P 金币，并且指定的计息假设成立，余额与假设利息分别怎样变化？** 不选择买谁、卖谁或是否购买，不输出操作指令，不将候选升级为真实局面。代码仅依赖 Python 标准库。

## 1. 规则来源与证据限制

本轮优先检索腾讯游戏官方公开页面，但没有取得足以证明当前国服本局完整计息规则的正文。不能据此声称官方未发布，也不能将网页检索日期当作本机版本核验。

| 来源 | 本轮读到的内容 | 如何使用 |
| --- | --- | --- |
| [TapTap：金铲铲之战黑话术语科普！](https://www.taptap.cn/moment/540934479900837024)，2024-05-18，作者“戏仔-金铲铲” | 正文说明每满10金币增加1金币利息，通常封顶5金币，并列出特殊情况。 | **历史社区二手资料，不是游戏官方**；帖子含交易推广。本模块只据此提供显式的10/1/5假设，不采用其中策略或推广内容。 |
| [Riot：Gizmos & Gadgets 强化机制说明](https://teamfighttactics.leagueoflegends.com/en-us/news/game-updates/gizmos-gadgets-set-mechanic-overview-hextech-augments/)，2021-10-21 | 历史TFT官方说明中的特定强化，可将利息上限提高到70金币对应7金币。 | 一手的**例外存在性**证据；不是当前国服强化、名称、数值或生效证明，不自动生成7金币上限规则。 |
| [Riot：Hyper Roll](https://teamfighttactics.leagueoflegends.com/en-sg/news/game-updates/teamfight-tactics-hyper-roll/)，2021-04-14 | 历史TFT官方模式说明中没有利息收入。 | 一手的**模式差异**证据；不证明本机处于该模式，也不据此自动设置零利息。 |

以上页面正文均于2026-11-02读取。公开引用的简短元数据由 `source_catalog()` 返回，不保存第三方全文或图片。国际服历史来源只能说明规则需要参数化，不能给国服本局补齐缺失值。

当前本局的地区、模式、赛季、补丁/热修复、强化/特殊效果、计息基数、时点和到账顺序均未由本模块确认。10/1/5只以 `ten_gold_five_cap_hypothesis()` 或命令行 `--rule historical-10-5` **显式选择**。省略规则不默认套用。

## 2. 计算合同

输入的规则假设包括正整数档距 `gold_per_step`、正整数每档收益 `interest_per_step` 和显式非负整数上限 `max_interest`。在输入有效且价格不超过候选余额时，分别计算：

```text
after_gold = candidate_gold - hypothetical_price
uncapped_steps(g) = g // gold_per_step
interest(g) = min(uncapped_steps(g) * interest_per_step, max_interest)
interest_delta = interest(after_gold) - interest(candidate_gold)
```

`uncapped_step_count` 是未封顶档数；`interest_gold` 是封顶后假设利息。二者分别输出，因为61→59可能跨过一个原始档位，却仍在5金币利息上限以上。`interest_effect` 只可能为 `reduced`、`unchanged` 或 `null`，不表达“值得买”或“没有成本”。

每个价格都从同一 G 出发，顺序及重复项保留，**互为独立替代项，不是连续购买清单**。不计算多个物品的组合预算。

| 输入情况 | 计算结果 |
| --- | --- |
| 金币为 `candidate`，来源明确，价格有效，规则假设有效 | `conditional`；给出扣款后余额、前后假设利息、差值和条件文案。 |
| 金币未知、冲突、不可读、过期、不适用，或仍有未解决的候选列表 | 该金币的所有比较阻断；不选候选、不平均、不把未知填成0。即使 `value` 非空也不计算。 |
| 金币/价格为负数、布尔、浮点、字符串、NaN/Infinity或超范围整数 | 阻断相关计算；不从“31”“3l”等原始文本猜数字。 |
| 单项价格未知、冲突、过期或无效 | 只阻断该项，其他独立项可继续。 |
| 价格大于候选金币 | `insufficient_candidate_gold`；`within_candidate_balance=false`，买后余额及所有利息字段均为 `null`，不输出负余额。 |
| 规则缺失、未知、冲突、撤销、参数不全或依据引用缺失 | `cash_only`；仍可做明确标注的单笔扣款算术，但所有利息字段为 `null`，不声称利息不变。 |
| 显式 `max_interest=0` | 可作“假设无利息”的计算；与规则缺失区分，不自动映射到任何真实模式。 |
| 价格0、金币0、花光余额、刚到档位、封顶前后 | 按整数公式计算；价格0不证明本局存在免费购买。 |

数值仅接受 `0..2**53-1` 内的 Python `int`（正参数从1开始），方便后续 JSON/中文界面在 JavaScript 中保持整数精度。上限 `None` 表示不完整，不代表“不封顶”；本版不支持未封顶/非线性/税费/负收益规则。此范围是工程输入限制，不是游戏金币上限。

## 3. 可导入函数

在项目根目录使用 `PYTHONPATH=src`，或由调用方配置模块路径；无需新增依赖或安装步骤。

```python
from jcc_economy_lab import (
    GoldCandidate, HypotheticalPrice, InterestAssumption,
    analyze_purchases, ten_gold_five_cap_hypothesis,
)

result = analyze_purchases(
    GoldCandidate(value=31, origin="synthetic", source_ref="synthetic:example-31"),
    [HypotheticalPrice(1, "假设对象A"), HypotheticalPrice(2, "假设对象B")],
    rule=ten_gold_five_cap_hypothesis(),
)
for comparison in result["comparisons"]:
    print(comparison["text_zh"])
```

产生以下两种情况，**均为合成输入**：

- 31金、花1金→余30金；10/1/5假设下利息3→3，不变。
- 31金、花2金→余29金；同一假设下利息3→2，少1金。

调用方可自行声明另一份假设，例如 `InterestAssumption("toy-cap7", 10, 1, 7, "synthetic_hypothesis", "纯合成的上限敏感性比较。", ("synthetic:toy-cap7",))`。相同61金、花2金时，上限5的假设利息为5→5，上限7则为6→5。该对比证明参数会改变算术结论，不证明本局有某个强化。

`GoldCandidate` 必须声明 `origin` 和 `source_ref`。支持 `synthetic`、`manual_hypothesis`、`replay_candidate`、`live_candidate`；后两者只代表来源类型，仍是机器候选。`status` 仅接受 `candidate` 进行计算；`recognized`、`verified` 等其他状态不能直接消费。`alternatives` 非空时一律阻断，由上游先处理分歧。本模块不核实引用内容，也不读取引用所指文件。

`HypotheticalPrice` 可用整数简写，仍只是假设；带状态时须为 `hypothesis`。规则只支持 `sourced_hypothesis` 或 `synthetic_hypothesis`，二者始终未与本局绑定。

## 4. JSON候选接口与17A后续衔接

`analyze_request(request: dict) -> dict` 接受本模块的独立小请求，不依赖或修改共享 GameState Schema。顶层仅有 `gold`、`prices` 和可省略/置空的 `rule`。示意如下；所有数字仍是构造数据：

```json
{
  "gold": {
    "value": 31,
    "origin": "replay_candidate",
    "source_ref": "synthetic:future-candidate-adapter-example",
    "status": "candidate"
  },
  "prices": [1, {"value": 2, "label": "假设对象B", "status": "hypothesis"}],
  "rule": {
    "assumption_id": "toy-10-1-5",
    "gold_per_step": 10,
    "interest_per_step": 1,
    "max_interest": 5,
    "basis_kind": "synthetic_hypothesis",
    "basis_note": "纯合成参数；未核实本局规则。",
    "source_refs": ["synthetic:future-adapter-rule"],
    "status": "hypothesis"
  }
}
```

未知字段、缺少必需结构或伪核验开关使请求整体 `blocked`；不静默忽略 `verified` 或 `production`。格式合法但字段未知/冲突时保留对应阻断语义。命令行还拒绝重复 JSON 键及非有限数字。

总控后续可从17A产物中**显式挑选金币机器候选**，保留帧/候选记录引用，转成本请求；状态无法对应、金币身份不明确、来源过期或存在冲突时，继续传 `unknown`/`conflict`/`stale`，不要选第一个数字或沿用旧值。不能仅凭候选字段名或机器自报置信度判定已经识别正确。上面不是对17A当前产物字段结构的描述，本轮没有读取真实产物或完成两侧集成。

结果中：

- `status` 为 `conditional`、`cash_only`、`partial` 或 `blocked`；逐项原因见 `comparisons[].issues`。只要混有阻断/超额项，部分有效结果以 `partial` 表达。
- `confirmed_game_state=false`、`match_rule_binding=unverified`、`settlement_binding=unbound`、`execution_eligible=false` 和 `strategy_recommendation=false` 始终保留，没有生产放行开关。
- 每项 `text_zh` 自带来源类别、条件数字和限制；完整界面还须展示规则前提及 `uncertain_premises_zh`，不能裁成“可买”或“不会亏”。

核心函数无文件、网络、设备、时钟、后台任务或全局候选缓存。输出是独立构造的 JSON兼容数据；返回值修改不会更改输入或后续调用。`source_ref` 只是可追溯标识，不创建有效期：上游须在新帧、买卖、刷新、强化变化、结算或断连后丢弃旧结果并重新计算。本模块没有自动撤回旧界面的能力。

## 5. 命令行和验证

在项目根目录：

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_economy_lab --demo
PYTHONPATH=src .venv/bin/python -B -m jcc_economy_lab --gold 31 --prices 1 2 32 --rule historical-10-5
PYTHONPATH=src .venv/bin/python -B -m jcc_economy_lab --gold 31 --prices 2 --json
PYTHONPATH=src .venv/bin/python -B -m jcc_economy_lab --request-json '{"gold":{"value":31,"origin":"synthetic","source_ref":"synthetic:inline"},"prices":[1,2]}' --json
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s src/jcc_economy_lab/tests -v
```

第三、四条没有规则，故仅扣款、不判断利息。命令行不提供文件路径读取、设备或截图参数。`--gold` 始终标为手工假设；`--demo` 是内置合成输入。退出码0表示有条件算术/仅扣款/部分结果，1表示没有可计算项，2表示命令行或JSON语法错误；0不代表本局事实确认或建议验收。

合成测试覆盖有变化/无变化、超额、0/10/20/50附近边界、花光、不同档距/收益/上限及零利息、未知/冲突/过期/不可读、非法数值、规则缺失、来源缺失、独立比较、结果隔离和命令行。另对3组参数、0..101金币及每个可支付整数价格检查余额守恒、利息不随花费增加、封顶范围等性质。测试不访问真实样本或平板；通过不等于识别准确、回合预测正确、实时可用或稳定上分。

## 6. 明确不计算的内容

本版没有绑定真正的回合事件，因此“少1金”只指**同一假设计息函数在两个余额上的差值**。若结算前发生其他收入/支出、胜场奖励到账、模式切换、强化生效或计息基数冻结，真实差值可能不同。不把该差值称为本轮实际损失，不计算下一轮总金币，也不作长期复利预测。

本版不核实商店对象、价格、持牌、出售折价、合星、容量和操作窗口，不评估战力、保血、对子机会或最佳买卖。后续正式策略须另行核实所需输入、规则与生命周期；本模块不替代12A的保守证据边界，不修改其规则库或14A提示原型。
