# 配方可行性候选原型 v1

本原型只回答：**在调用方显式提供、版本完全一致、完整已知的闲置散件库存及配方中，哪些成品各自能够合成？**
候选均从同一份起始库存单独检查，可能争用散件。不表示可同时合成，不计算最大合成次数、成品强度、最佳装备、
装备分配、合成位置、拆卸或特殊效果，也不产生设备操作。

输入未知、未核验、冲突、版本不匹配或算法未成功运行时，返回 `status: "unknown", candidates: null`。
成功检查且没有可行项时才返回 `status: "evaluated", candidates: []`；这个空结果只覆盖调用方提供的配方集。

## 复用决策与许可

采用 18B 已复核的 [ndh0408/tft-advisor 固定提交](https://github.com/ndh0408/tft-advisor/tree/8380b6dbeffe9edade7b198d8063bc3e18917b6d)
`8380b6dbeffe9edade7b198d8063bc3e18917b6d` 中 [`craftableFrom`](https://github.com/ndh0408/tft-advisor/blob/8380b6dbeffe9edade7b198d8063bc3e18917b6d/src/engine/recipes.mjs#L31)。
原模块第 31–44 行连续片段（原注释和完整函数）原样保存在
[`vendor/craftableFrom.mjs`](../../src/jcc_recipe_candidate/vendor/craftableFrom.mjs)。

Python 负责校验和数据形状转换，启动 Node，由 `bridge.mjs` 直接导入原函数；**没有翻译或另写配方可行性算法**。
只提取实际采用的函数，避免引入未使用的游戏 ID 白名单和配方 builder。
未采用 `buildRecipes` 的白名单、缺输入默认为空、非法行静默过滤、重复 ID 后写覆盖行为。

完整原 MIT 许可、Copyright (c) 2026 ndh0408、固定来源、文件摘要与适配差异分别保存在：

- [`vendor/tft-advisor-LICENSE.txt`](../../src/jcc_recipe_candidate/vendor/tft-advisor-LICENSE.txt)
- [`vendor/SOURCE.md`](../../src/jcc_recipe_candidate/vendor/SOURCE.md)
- [`vendor/manifest.json`](../../src/jcc_recipe_candidate/vendor/manifest.json)

原始完整文件 SHA-256 为 `361cd569b129a66356fe66565b5bbb6f40dad6c8eadb57a702a0a118c0ab5c9d`；
采用片段为 `ba5620cb0e1ca229fb0dbc5aa173ef9ba9a4e0cbfe5b795a37b10d5dff7c6c59`，566 字节。
运行前校验片段摘要；测试另核对完整原 LICENSE 摘要。
完整原文件未打包，固定公开链接可用于核对；没有复制游戏数据、图标、抓取器、私人材料或设备标识。
根 `THIRD_PARTY_NOTICES.md` 留给总控串行整合，本任务未改动。

## 运行与入口

运行要求：Python >=3.10、Node >=18，使用标准库，无需安装 npm 或 Python 包，不联网取数据。
实际验证环境为 Python 3.14.7 / Node v26.5.0 / macOS arm64；未逐一复测最低版本。
每次成功通过输入校验后启动一个 Node 进程，超时为 5 秒；未测实战或实时提示耗时。
Node 缺失、启动失败、超时、非零退出、源码不符或输出异常都返回 `unknown`。

Node 通过调用方进程的 `PATH` 查找。在切换子进程工作目录前，将找到的执行文件路径转为相对于调用方
当前目录的绝对路径，因此 `bin`、`./bin` 或空目录项等相对搜索路径不会因子进程改在包目录运行而失效。
这只固定路径解释，不认证执行文件；本接口没有显式可信 Node 路径或 Node 摘要选项。

**运行环境信任前提：调用方须信任 `PATH` 选中的 Node、`bridge.mjs`、本包文件及宿主环境。**
源码摘要只核对采用的原算法片段与包内预期摘要是否一致，不认证 Node、桥接、其他包文件或宿主。
输出校验检查结构、配方对应关系、去重和顺序，不独立复算可行性；对抗性运行时仍可能返回结构合法的错误候选，
也可能遗漏候选或返回空列表。`evaluated` 的计算语义依赖上述信任前提，不能解释为对运行环境或候选真伪的认证。
子进程仅保留标准 `PATH` 环境变量、不继承 Node 预加载选项，这不是沙箱或对恶意可执行文件的隔离保证。
此处说明的是接口边界，没有据此认定正常 Node 运行受到污染。
`evaluated` 和 `unknown` 的 `notes_zh` 均包含这项信任前提。

从项目根目录运行纯虚构演示：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m jcc_recipe_candidate --demo
```

输出完整的虚构请求，便于调用方查看所有必需字段：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m jcc_recipe_candidate --print-synthetic-request
```

CLI 只接受一个显式入口：`--demo`、`--print-synthetic-request`、`--stdin`、`--input <请求文件>`。
结果写到标准输出，不写输入、库存或其他文件。`evaluated` 退出码 0，`unknown` 退出码 2；命令行参数错误也为 2，
参数错误由 argparse 显示用法，不属于 JSON 业务响应。`--print-synthetic-request` 不运行算法。

Python 调用：

```python
from jcc_recipe_candidate import analyze_request, analyze_json
from jcc_recipe_candidate.synthetic import synthetic_request

request = synthetic_request()  # 仅用于离线联调；所有 ID、版本、配方均为虚构
result = analyze_request(request)
assert result["status"] == "evaluated"
assert [row["item_id"] for row in result["candidates"]] == ["fiction_xx", "fiction_xy"]
assert result["simultaneous_feasibility"] == "not_evaluated"

request["inventory"] = {"status": "unknown"}
blocked = analyze_request(request)
assert blocked["status"] == "unknown" and blocked["candidates"] is None

# 对原始 JSON 使用 analyze_json(text)，才能在反序列化前拒绝重复键。
```

`analyze_request` 接收普通 Python `dict` / `list` 及 JSON 基础值，不修改输入，也不自动读取任何目录或资料库。
导入包不启动 Node、不读取库存或设备。`analyze_json` 拒绝 JSON 重复键（任意层级）、NaN、Infinity 及格式错误；
Python 已经构成的 dict 无法恢复先前被覆盖的重复键，调用方不得先用宽松解析器丢失冲突。
`bridge.mjs` 是内部入口，只接收 Python 已完成校验及规范化的载荷，不是公开的未校验请求入口。

## 请求格式

顶层仅允许 `format_version`、`version`、`inventory`、`catalog` 四个必需字段。
`format_version` 固定为 `jcc-recipe-candidate-request/v1`。
已知对象的字段集合必须与本规格一致；未知字段不被忽略，不能传入额外标志提升可信度或放行操作。
任一块未知可以直接给 `{"status":"unknown"}`，整次结果为 `unknown`。

三个已知对象共享以下字段：

| 字段 | 规则 |
| --- | --- |
| `status` | 必须显式为 `"known"`。`candidate`、`conflict`、`stale`、空值等一律不能计算。 |
| `binding` | 下述六维版本对象，必需且三个对象完全相同，大小写敏感。 |
| `evidence` | 下述来源声明，必需；三个对象必须同为合成或同为经核验声明。 |

`version` 对象仅包含以上三个字段，表示本次请求的预期版本。

`binding` 必须恰含：`game`、`region`、`mode`、`season`、`patch`、`rules_revision`。
每个值是长度 1–128 的 ASCII 标识，匹配 `[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}`；不猜测任何维度。
`rules_revision` 应定位配方及热修订的具体核验版本，不能只填客户端版本来代替。
`unknown`、`unverified`、`unconfirmed`、`pending`、`latest`、`current`、`unspecified`、`none`、`null`、
`missing`、`tbd`、`any`、`all`（不区分大小写）均不是有效已知版本值。

来源声明只接受两种形状：

```json
{"kind":"synthetic","refs":["synthetic:recipe-candidate-v1"]}
```

```json
{"kind":"verified","refs":["synthetic:verification-source"],"verification_ref":"synthetic:verification-record"}
```

第二例的引用也明确是虚构测试引用，不是真实核验。实际调用方必须填自己的核验记录。
`refs` 须有 1–32 个不重复的引用；引用及 `verification_ref` 是长度 1–1024 的非空文本，无首尾空白、
Unicode `Cc` 控制字符（C0、DEL、C1）或 `Cs` 代理码点。允许正常 Unicode 文本、组合字符、非 BMP 字符及
emoji 中的格式字符（如 `U+200D`）；不做 Unicode 归一化或改写。
`verified` 必须另有非空 `verification_ref`。引用可以指向调用方已保有的记录，原型不会自行抓取或认证。

**`verified` 是调用方的显式声明，不是本原型出具的核验结论。** 原型只验证结构、来源是否提供、状态及版本的一致性，
不能判断虚假声明，也未与现有证据库做真实性联查。所有输出固定 `input_verification: "caller_declared_only"`。
未核验的机器候选、混合来源资料和未知本局版本不可仅改状态后作为可信输入。

### 库存对象的额外字段

| 字段 | 规则 |
| --- | --- |
| `availability` | 必须为 `"unequipped_components"`，由调用方确认是可用的闲置散件；已装备、位置不明或可用性未明时未知。 |
| `complete` | 必须是布尔值 `true`，声明此范围库存完整已知；数字 1 无效。 |
| `entries` | 显式列表，每项恰含 `component_id`、`count`；最多 256 项。 |

`component_id` 必须在所提供的散件目录中，同 ID 只能出现一行；重复库存不求和。
`count` 必须是 0 至 `2^53-1` 的整数，布尔值、浮点数、字符串、负数、NaN、空值均无效。
这只是精确 JSON 数值边界，不代表真实游戏库存上限。
只有 `status: known`、`complete: true` 等条件全部成立后，列表未出现的目录散件才解释为数量 0。
显式空列表表示已知空库存；不完整观察、未识别和不可读状态不得这样编码。

### 配方目录对象的额外字段

| 字段 | 规则 |
| --- | --- |
| `component_ids` | 显式、无重复的散件 ID 列表，最多 256 项；不内置游戏 ID 白名单。 |
| `recipes` | 显式列表，最多 4096 项；每项恰含 `item_id`、`components`。 |

所有实体 ID 长度 1–64，匹配 `[A-Za-z][A-Za-z0-9_.:-]{0,63}`，大小写敏感。
拒绝 JavaScript 原型属性名（如 `constructor`、`toString`、`__proto__` 等，完整集合见 `core.py`），
以适配原函数使用普通对象计数的行为。以字母开头也避免数值键改变原函数的遍历顺序。

`components` 必须是恰有两个目录内散件 ID 的列表，同类双件如 `[piece_x,piece_x]` 合法；
空配方、长度 1/3、非列表和不存在的组件都使整次结果为未知，不静默过滤。
`item_id` 非空、合法且不与散件 ID 重叠；不处理以成品为组件的链式或循环配方。

重复成品 ID（即使配方相同）、同一无序散件对再次出现（包括顺序反转、对应不同成品）、重复散件 ID 均阻断。
不会按顺序覆盖、自动合并、静默丢弃或选一个来源。若游戏规则需要一个散件对产生多个结果，应先扩充显式上下文，
本规格不支持这种歧义。

可显式给空配方列表；成功结果只说明“所提供配方集中没有可行项”，不等于全游戏没有可合成装备。
容量 256/4096 是本原型的资源边界，不是游戏事实。

## 输出契约

输出格式为 `jcc-recipe-candidate/v1`，关键字段如下：

| 字段 | 含义 |
| --- | --- |
| `status` | `evaluated`：已对提供配方成功计算；`unknown`：输入或执行条件未满足。 |
| `candidates` | `unknown` 时为 `null`；`evaluated` 时为列表，可为空。每项是单独可行候选。 |
| `candidate_semantics` | 固定 `independently_feasible_alternatives`。 |
| `candidate_order` | 固定 `input_recipe_order_not_rank`；维持输入配方顺序，不是优先级。 |
| `simultaneous_feasibility` | 固定 `not_evaluated`，即使多项出现也不表示可以一起合成。 |
| `inventory_consumed` | 固定 `false`，不扣减或持久化库存。 |
| `catalog_scope` | 固定 `caller_supplied_recipes_only`。 |
| `strategy_recommendation` / `execution_eligible` | 永远 `false`，不能据本模块直接发建议或操作。 |
| `input_verification` | 固定 `caller_declared_only`，不自动认证外部记录。 |
| `data_kind` / `binding` / `evidence` | 成功时为校验后的来源类型、版本及三块来源快照；未知时 `null`。 |
| `inventory_snapshot` | 成功时为完整已提供计数的独立副本，不截断原数量；未知时 `null`。 |
| `issues` | 错误列表，含稳定 `code`、输入 `path`、中文 `message_zh`；成功为空。 |
| `notes_zh` / `algorithm` | 明确备选语义、运行环境信任前提和固定算法来源；摘要不认证 Node、桥接、本包文件或宿主，不回显任意错误文本或 Node stderr。 |

候选项包含 `item_id`、原输入的两份 `components`、展示所需数量的 `required_counts`，
以及固定 `feasibility: "individually_feasible"`。不返回合成次数或任何评分。

虚构例：库存 `X×2,Y×1`，配方 `XX、XY、YY`，会返回 `XX、XY` 两项。
两项各自可行，但一起需要三份 X，现有库存不足。本模块不输出同时合成结论。
库存 `X×4` 仍只返回一个 `XX` 候选，不能把候选条数解释为可做件数。

内部数量展开最多保留每类两份，以避免按巨大计数分配内存；所有合法配方恰需两份，因此不改变单项可行性。
这是输入形状适配，原作者的“持有计数与每项配方需求比较”逻辑保持原样。Python 不再计算第二套可行性结果。

常见错误码：`input_not_known`、`version_invalid`、`version_mismatch`、`evidence_insufficient`、`inventory_incomplete`、
`inventory_unavailable`、`count_invalid`、`recipe_pair_invalid`、`recipe_id_duplicate`、`recipe_pair_duplicate`、
`inventory_id_duplicate`、`shape_invalid`、`json_invalid`；具体路径可区分输入位置。
运行错误以 `upstream_` 开头，和“成功计算、零候选”严格区分。

## 合成验证与复现

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m unittest discover -s src/jcc_recipe_candidate/tests -v
```

2026-11-02 的 19E 边界修复后复跑 48 项测试全部通过（原 42 项及新增 6 项）。
测试没有加载现有游戏资料、私人数据或任何设备模块，也没有连接平板。覆盖包括：

- 18B 的 32 组虚构库存（X/Y 各 0–3，Z 0–1）逐组与独立“移除库存副本”参考对照；这不扩展为真实配方覆盖。
- 一份/两份同类散件、混合散件不足、共用库存互斥备选、四份散件仍只列一项、巨大精确计数。
- 显式空库存与未知库存区分、未知/缺失/冲突/过期输入、全部六维版本不一致、未提供来源或核验记录、混用来源类型。
- 空配方、缺组件、未知组件、非法 ID、原型属性名、重复成品及冲突倒序、无序对冲突、重复库存、链式合成。
- 严格 JSON 重复键、非有限数、额外执行标志、CLI/stdin/JSON API、返回结果与输入互不修改。
- Node 缺失、超时、非零退出、源码不可读/摘要变更、非法输出等均保持未知；完整 MIT 许可与采用片段摘要核对。
- 调用方 `PATH` 含 `bin`、`./bin`、空目录项三种情况时，实际通过指向正常 Node 的临时符号链接运行 CLI，均取得相同虚构候选。
- 全部 65 个 Unicode `Cc` 码点分别覆盖三块来源的 `refs` 和 `verification_ref`（390 个拒绝子例），均在调用后端前阻断；
  CLI 另验证字面及 JSON 转义的 `U+0080`；中文、组合字符、非 BMP emoji、emoji 中的 `U+200D` 及阿拉伯文原样保留。
- 仅用故障注入返回结构合法但不可行的 `YY`，说明输出校验不认证计算语义，并核对成功和未知输出都含运行环境信任说明；
  此测试没有替换本机 Node，也不表示正常运行受到污染。

其中成功算法路径实际调用 Node 原函数，输入错误路径用探针确认未调用后端；
运行失败路径使用合成故障注入。没有安装整套上游应用，没有实机数据、准确率、策略效果、上分或真实时延结论。

## 后续联调方法与边界

调用方从自己的版本记录、配方核验记录和完整库存观察构造本地请求，保持三块版本和来源类型一致。
对无法证明库存完整、物品闲置、配方适用或证据齐全的情况，直接传未知，不造空数组。
本模块尚未接入 `jcc_catalog`、GameState、视觉识别、提示层或设备层，也未认证其核验记录。

联调先用 `synthetic_request()` 或 `--print-synthetic-request` 打通 JSON 传递，再接入调用方经核验的版本化材料；
必须同时读取 `status`、`candidate_semantics`、`simultaneous_feasibility` 和来源声明，不能只读取候选数组。
跨帧绑定、新鲜度、库存唯一性、当前可操作性仍由外层负责，不在本模块中推断。
需要同时做多个成品时，应单独增加共享库存分配与目标选择步骤，不能直接串行执行本列表。

部署或打包时须保留整个 `jcc_recipe_candidate` 目录中的 `bridge.mjs`、原算法片段、许可及来源记录；
只复制 Python 文件不能运行，也会丢失第三方许可材料。该独立模块可用 `PYTHONPATH=src` 调用，尚未修改项目打包配置。
模块与规格写入范围为 `src/jcc_recipe_candidate/` 和本规格，19E 修复报告另存于
[`资料/开发/19E_配方边界修复.md`](../../资料/开发/19E_配方边界修复.md)；
未修改共享模块、台账或公共第三方总声明，未执行 Git。
