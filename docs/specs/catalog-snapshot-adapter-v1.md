# 本地候选快照适配器 v1

`jcc_catalog_snapshot` 是一个离线适配器：它接收调用方已经在本地取得的结构化英雄、羁绊和装备记录，建立候选映射：显示名 → 一个或多个稳定键、`hero_key → trait_key[]`、`trait_key → thresholds[]`、`item_key → component_key[]`。

它不联网、不下载图标或原画、不执行来源脚本、不缓存或写入游戏资料。仓库只提交代码和虚构测试，不提交原始游戏数据、真实请求、截图、设备资料或来源全文。

## 输入与边界

输入版本为 `jcc-catalog-snapshot-request/v1`，必须含有：

- `source`：调用方声明的 `game / region / mode / season / version`，以及 HTTPS 来源 URL、带时区的获取时刻和原始内容 SHA-256；
- `traits`：`trait_key`、显示名和严格递增的正整数档位；
- `heroes`：`hero_key`、显示名和已存在的 `trait_keys`；
- `items`：`item_key`、显示名及同一快照内存在的 `component_keys`。

来源 SHA-256 只检查为小写 64 位十六进制字符串，是调用方的声明。适配器没有原始 payload、不能重新计算摘要或证明摘要与输入 records 对应，因此它不是独立来源认证。它拒绝原始 URL 空白、常见中英文版本占位（如 `unknown`、`N/A`、`未知`、`待核实`、`待核验`、`最新版本`）、重复稳定键、无效层级、缺失羁绊或配方组件、装备自引用或多件循环、重复 JSON 键、非有限数和未完整结构，返回 `status: "unknown"`；两个相同散件组成一件装备仍是合法配方。它不尝试穷尽自然语言占位词，也不因此验证客户端。同名英雄或装备不是自动合并或自动选择，而是保留全部候选稳定键。

成功结果仍为 `status: "candidate"`，固定为 `source_version_applicability: "unknown"`、`target_client_binding: "unknown"`、`decision_eligible: false` 和 `action_advice: null`。即使来源被调用方标为 `official_candidate`，其模式、赛季和版本也只是来源声明；无法证明当前平板、模式、补丁、热修或对局实际匹配。任何行动建议须由另一条真实客户端绑定流程独立完成，不能靠调用者自填字段升级。

## 上游借鉴与访问边界

结构选择借鉴 [TanzWen/jcc-tft-simulator 的固定 `collector.py`](https://github.com/TanzWen/jcc-tft-simulator/blob/a9cec17422e1742d67d09fdba7b51ce4be165c68/src/jcc/collector.py)：先读官方配置中的模式/赛季，再由版本索引选择明确版本，最后载入英雄、羁绊和装备数据。该上游代码为 [MIT](https://github.com/TanzWen/jcc-tft-simulator/blob/a9cec17422e1742d67d09fdba7b51ce4be165c68/LICENSE)；本模块没有复制其代码或分发其数据，只实现独立的本地输入合同。

本轮只读核对的本地 robots 快照为 `资料/版本数据/来源快照/官网_爬取规则.txt`，其文件记录 `User-agent: openai` 为 `Disallow: /`；项目版本资料元数据的获取时刻为 `2026-10-22T07:01:40.274Z`，并非本轮抓取。本轮也无法通过受支持浏览器访问 `jcc.qq.com`、`game.gtimg.cn` 的 robots、`basicConfig.js` 或 `versiondataconfig.js`，因此没有请求任何官方 payload、没有创建私有下载快照，也无法核实官方当前模式、赛季、版本、许可或设备补丁。未来只可在来源访问条件明确允许、独立来源许可清楚且平板实际版本另有证据时，由独立绑定流程处理。

## 验证

```bash
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -B -m unittest \
  src/jcc_catalog_snapshot/tests/test_core.py -v
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -B -m jcc_catalog_snapshot --demo
```

测试仅证明虚构完整快照中的映射、同名歧义、结构拒绝、来源版本声明边界、输入不变性和严格 JSON 行为；不证明真实资料、策略、识别、实时性或上分效果。
