# 玩家实时提示体验 v1

代码入口：[jcc_human_coach](../../src/jcc_human_coach/)。它面向“玩家自己操作，教练在旁提示”的目标，负责将已经形成的短建议卡以可阅读的方式显示、刷新和撤回。它不读取平板、不会点击或拖动，也不运行 OCR、模型、阵容选择、商店判断或装备配方计算。

本次交付是一个**离线回放例子演示**，不是真实实时流或实战成绩。两个场景来自本局独立复盘已确认的画面事实；本模块没有自行识别它们，也不能默认下一局仍适用。另外三张战力优化卡明确是“假设演示”。它依次播放六种玩家可见状态：

1. “2-1 回放例子 · 自然庇护所”：若玩家已经选择该强化，提示补上前排并围绕植物考虑站位。
2. “2-5 回放例子 · 可上 5、场上 4/5”：提示先补满人口，再比较羁绊。
3. “假设演示 · 羁绊补位”：若场上有奥恩和霞、备战有阿利斯塔且有空位，上阿利斯塔后尝试 3 永恒之森；替换前先确认会失去什么。
4. “假设演示 · 装备合成与携带者”：若散件为暴风大剑和反曲之弓，巨人杀手可给后排物理携带候选霞；只在配方、携带者和空槽都同帧确认时显示。
5. “假设演示 · 阵容转型缺口”：若凑 2 迅捷射手需要替换 3 永恒之森，先显示失去的羁绊、换入棋子和金币代价，再考虑转型。
6. “提示已撤回”：未知新帧时清除上一条动作、理由和数字。

前两张卡标为 `replay_candidate`；“自然庇护所”的具体文本、可上人口和备战席情况是回放事实，不是当前游戏的自动识别。后三张卡标为 `synthetic`，标题直接写“假设演示”。因此整段离线示例播放固定 `simulation=true`，不能误读为实机成绩。它们不评价当前版本强度或保证玩家应照做。所有候选内容都为调用方声明，`source_authenticated=false`、`strategy_verified=false`、`execution_eligible=false` 固定为假。`live_candidate` 仅代表未来调用方可声明其来自实时流，不能被本模块升级成已认证实时画面、准确识别、版本正确或最佳策略。

## 生成并查看私有演示

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_human_coach \
  --demo-output "$HOME/Library/Application Support/jcc-human-coach-demo-001"
```

输出目录必须尚不存在、位于项目外，且其父目录的真实解析路径不得在项目内、必须归当前用户所有且权限不得对组或其他用户开放。生成内容为：

- `player-coach-demo.html`：无网络连接的单页卡片。可点“自动演示”，也可逐状态查看。
- `report.json`：六条视图的结构化记录：两条回放条件例子、三条明确标注的合成假设和一条未知撤回。

HTML 用 `textContent` 写入文案并用 `replaceChildren()` 完整替换卡片；撤回时页面不保留旧对象、动作、理由或数字。每次选卡建立独立本地倒计时，到期即改为 `candidate_expired`，旧卡定时器在切卡时取消，不能撤掉更新卡。它不加载图片、外部脚本、字体、追踪或遥测。随模块提供的 Node 生命周期回归会执行生成页的渲染脚本，检查刷新后旧文案不残留、未知撤回后不保留上张转型提示；真实浏览器的字号、遮挡和屏幕可见性仍须在下一阶段独立复核。页面没有真实断流监听；断连只能由未来实时适配器显式发布，当前离线播放不能声称已实测断流撤回。

## 输入、刷新与撤回

`HumanCoachStore` 接收同一 `stream_id` 上严格连续的 `CoachEvent`：

| 字段 | 约束 |
| --- | --- |
| `source_kind` | `synthetic`、`replay_candidate` 或 `live_candidate`；只是调用方分类。 |
| `sequence / frame_received_ms / frame_id` | 必须严格推进；倒序、跳号、重复 `frame_id` 或换流会隐藏旧卡。重复 ID 拒绝调用方的明显自相矛盾重放，但不能认证 ID 对应真实新画面。 |
| `emitted_ms / expires_ms` | 以同一调用方给出的毫秒时钟计，最长 10 秒；到期自动撤回。 |
| `status` | `candidate_visible` 才能携带短建议；`unknown/stale/conflict/scene_changed/disconnected/expired` 都清空旧卡。 |
| `CoachAdvice` | 只含类别、标题、动作短句和理由短句；没有坐标、设备 ID、图像、账号或自动执行字段。 |

这是一条展示合同，不能证明生产者真的获得新帧，或其时间、流、策略依据来自可信源。未来接入必须复用并扩展已有职责：

- `jcc_device.latest.LatestFrameStream` / `jcc_realtime_probe` 负责帧接收、流序列和上游失效；其主机接收时间不等于设备采集时间。
- `jcc_realtime_overlay_bridge` 与 `jcc_timed_overlay` 已有最新帧、有限租期、浏览器刷新和撤回统计能力。它们当前的事件没有玩家建议内容，不能把本模块直接当作已接通。
- 版本绑定的策略和识别组件必须先产生有来源、同帧、未过期的候选，再适配为本合同；任一关键字段未知、冲突、场景切换、断连或新帧到达时，旧策略卡均要撤回。

后续实机验收应分开测：画面到候选、策略到卡片、浏览器卡片出现、玩家实际看到并做出选择。不能用本页的合成倒计时、局部 OCR 或自动操作局的成绩替代。

## 测试

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s src/jcc_human_coach/tests -v
node --test src/jcc_human_coach/tests/test_web_lifecycle.cjs
node src/jcc_human_coach/tests/test_browser_visibility.mjs
PYTHONPATH=src .venv/bin/python -B -m jcc_human_coach --help
```

测试只使用内置回放例子卡片和临时私有目录，覆盖刷新、未知撤回、到期、乱序/非法输入、实时候选不升级为认证或执行资格，以及离线 HTML/报告不含网络引用。通过不代表真实策略、版本、识别准确率、提示速度或玩家上分效果。
