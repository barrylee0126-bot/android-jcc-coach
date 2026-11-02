# tft-advisor：固定来源与采用范围

本目录采用 [ndh0408/tft-advisor](https://github.com/ndh0408/tft-advisor) 固定提交
`8380b6dbeffe9edade7b198d8063bc3e18917b6d` 的 MIT 代码。Copyright (c) 2026 ndh0408。
完整原许可原样保留在同目录 `tft-advisor-LICENSE.txt`；复制或分发本目录时应同时保留。

采用内容为原始 [`src/engine/recipes.mjs` 第 31–44 行](https://github.com/ndh0408/tft-advisor/blob/8380b6dbeffe9edade7b198d8063bc3e18917b6d/src/engine/recipes.mjs#L31)：
包括原注释及 `export function craftableFrom` 的完整函数体。`craftableFrom.mjs` 与该片段逐字节相同，
仅从原模块中截取该连续片段；没有修改函数、翻译为 Python、调整匹配方法或增加内部逻辑。
Python 通过标准输入/输出调用 Node，再由 `bridge.mjs` 直接 import 此函数。

没有复制原模块未使用的 `COMPONENTS` 游戏标识白名单、`buildRecipes`、其余导出、抓取器、
游戏配方、图标、数据集或整套应用。片段仅接收调用方的数据；与上游数据来源无连接。
本次没有主张任何游戏数据或素材的分发权，也没有采用上游的装备强度判断。

## 适配差异（均在原函数外部）

- 仅接受版本完全绑定、来源声明齐全、库存完整已知且标为可用闲置散件的请求。
- 配方从显式列表校验后转为原函数的 ID 字典；不调用有白名单、覆盖和默认空值行为的 `buildRecipes`。
  重复成品 ID、重复无序散件对、重复库存行均阻断，不合并或选择最后一条。
- 数量输入为计数。每类散件展开最多两份给原函数：合法配方恰需两份，因此不改变**单项是否可行**。
  完整数值保留在输出库存快照中；不计算最大合成次数或连续计划。
- ID 限制为 ASCII 标识并拒绝 JavaScript 原型属性名，避免原函数内部 `{}` 的继承属性影响计数。
- 未知输入不进入原函数。运行错误、超时或源码摘要不符都返回 `unknown`，不伪装成无候选。
- 原输出包装为各自可行的备选，保持输入配方顺序；不消费共享库存、不排序推荐、不授权操作。

## 来源核对

2026-11-02 用 GitHub 连接器按上述完整提交取得原源码和根 LICENSE，并与 18B 已复核的摘要核对。
完整文件及本地采用片段摘要见 `manifest.json`。完整原文件不随本原型复制；固定公开链接可供重建和核验。
测试会核对本地采用片段和原 LICENSE 的摘要；Python 运行前也校验片段摘要。

根许可：[固定 LICENSE](https://github.com/ndh0408/tft-advisor/blob/8380b6dbeffe9edade7b198d8063bc3e18917b6d/LICENSE)。
公共项目的第三方总声明见仓库根目录 `THIRD_PARTY_NOTICES.md`。
