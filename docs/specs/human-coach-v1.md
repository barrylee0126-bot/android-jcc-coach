# 玩家实时提示体验 v1

代码入口：[jcc_human_coach](../../src/jcc_human_coach/)。它面向“玩家自己操作，教练在旁提示”的目标，负责将已经形成的短建议卡以可阅读的方式显示、刷新和撤回。它不读取平板、不会点击或拖动，也不运行 OCR、模型、阵容选择、商店判断或装备配方计算。

本次交付是一个**完全虚构的离线演示**，不是真实实时流、回放识别或实战成绩。所有角色、阶段、强化、棋盘和策略文本都是渲染/撤回夹具，不能关联任何真实对局。它依次播放六种玩家可见状态：

1. “虚构例子 · 站位检查”：若虚构前排条件成立，比较前后排位置。
2. “虚构例子 · 人口检查”：有虚构可用单位时，提示考虑补满人口，再比较羁绊。
3. “假设演示 · 羁绊补位”：若虚构棋子和空位条件都成立，尝试补齐虚构羁绊；替换前先确认会失去什么。
4. “假设演示 · 装备合成与携带者”：若散件为暴风大剑和反曲之弓，巨人杀手可给后排物理携带候选霞；只在配方、携带者和空槽都同帧确认时显示。
5. “假设演示 · 阵容转型缺口”：若凑虚构羁绊乙需要替换虚构羁绊甲，先显示失去的羁绊、换入棋子和金币代价，再考虑转型。
6. “提示已撤回”：未知新帧时清除上一条动作、理由和数字。

六张卡全部标为 `synthetic`，标题直接写“虚构例子”或“假设演示”。因此整段离线示例播放固定 `simulation=true`，不能误读为实机成绩。它们不评价当前版本强度或保证玩家应照做。所有候选内容都为调用方声明，`source_authenticated=false`、`strategy_verified=false`、`execution_eligible=false` 固定为假。`live_candidate` 仅代表未来调用方可声明其来自实时流，不能被本模块升级成已认证实时画面、准确识别、版本正确或最佳策略。

## 生成并查看私有演示

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_human_coach \
  --demo-output "$HOME/Library/Application Support/jcc-human-coach-demo-001"
```

输出目录必须尚不存在、位于项目外，且其父目录的真实解析路径不得在项目内、必须归当前用户所有且权限不得对组或其他用户开放。生成内容为：

- `player-coach-demo.html`：无网络连接的单页卡片。可点“自动演示”，也可逐状态查看。
- `report.json`：六条视图的结构化记录：五条虚构候选和一条未知撤回。

HTML 用 `textContent` 写入文案并用 `replaceChildren()` 完整替换卡片；撤回时页面不保留旧对象、动作、理由或数字。每次选卡建立独立本地倒计时，到期即改为 `candidate_expired`，旧卡定时器在切卡时取消，不能撤掉更新卡。它不加载图片、外部脚本、字体、追踪或遥测。随模块提供的 Node 生命周期回归会执行生成页的渲染脚本，检查刷新后旧文案不残留、未知撤回后不保留上张转型提示；真实浏览器的字号、遮挡和屏幕可见性仍须在下一阶段独立复核。页面没有真实断流监听；断连只能由未来实时适配器显式发布，当前离线播放不能声称已实测断流撤回。

## 输入、刷新与撤回

`HumanCoachStore` 接收同一 `stream_id` 上严格连续的 `CoachEvent`：

| 字段 | 约束 |
| --- | --- |
| `source_kind` | `synthetic`、`replay`、`replay_candidate` 或 `live_candidate`；只是调用方分类。 |
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

测试只使用内置虚构卡片和临时私有目录，覆盖刷新、未知撤回、到期、乱序/非法输入、实时候选不升级为认证或执行资格，以及离线 HTML/报告不含网络引用。通过不代表真实策略、版本、识别准确率、提示速度或玩家上分效果。

## 25E：本机动态输入与浏览器刷新

本阶段新增的是一个可由未来只读回放/识别适配器持续写入的**本机回环**通路：匿名 JSONL 的新增完整行 → `HumanCoachStore` → `http://127.0.0.1:<port>/` → 浏览器短提示。它不读取平板、不会启动对局，也没有上传或自动操作。页面固定标为“仅回环送达；平板显示尚未接入/验收”：Chrome 结果只证明 Mac 本机页面的 DOM/绘制机会，不证明玩家在平板上看得到。

```sh
touch /private/tmp/jcc-human-coach-input.jsonl
PYTHONPATH=src .venv/bin/python -B -m jcc_human_coach \
  --input /private/tmp/jcc-human-coach-input.jsonl \
  --stream-id coach-stream-example --clock-domain-id host-mono-example
```

输入文件只处理启动后追加的完整行；每行必须是精确 JSON 对象，且只允许结构化字段：`schema_version`、`source_kind`、正交的 `evidence_status`、`stream_id`、`frame_id`、适配器连续 `sequence`、原始 `source_sequence`、`frame_received_ms`、`emitted_ms`、`expires_ms`、`status`、`reason`、`context_version`、`evidence_id`、`advice|null` 与 `observation|null`。`evidence_status` 是 `unverified/machine_candidate/independently_reviewed` 之一，不能由 `source_kind=replay_candidate` 推断。`advice` 只包含 `advice_id/category/title_zh/action_zh/reason_zh`；`observation` 独立包含 `observation_id/title_zh/facts_zh/unknown_zh`，显示为“仅观察，不提供建议”。可见候选必须二选一，不能混用。页面用 `textContent` 写入所有文本，不把建议作为 HTML 解释。

`sequence` 是适配器展示事件的连续序号；`source_sequence` 是上游原始帧/候选序号，二者明确分开保存，禁止为填补跳号重写原始帧。断连、静默、超时或页面故障只通过本机状态撤回，绝不伪造成新帧或新源序号。`GET /api/view` 给出 `stream_id`、`host_clock_domain_id`、`session_id`、`revision`、`render_token`、状态/原因、剩余时间/本地租期、原始绑定和 `advice|null`；跨客户端必须用 `session_id+revision+render_token` 拒绝旧回包。

可见的 replay/live 候选必须有原始 `source_sequence`，它相对同一流严格前进；未知控制/撤回事件可保持 `null`，绝不回填展示 `sequence`。`source_sequence` 回退、重复，或 frame/展示顺序冲突都会撤回。`machine_candidate` 只能使用独立 `observation`，不能携带 `advice`；replay/live 的建议还要求独立复核证据、上下文版本和证据 ID。`advice` 与 `observation` 必须恰有一个，任何附带对象即使无效也不会被忽略。

- 新候选只在同流 `sequence` 连续、`frame_received_ms` 前进、`frame_id` 未复用且源租期未过时显示。新卡取得新的 `revision/render_token`。
- `unknown/stale/conflict/scene_changed/disconnected/expired`、非法 JSON/字段、源文件变化、浏览器本地租期到期和本机时钟回退都会清除标题、动作、理由和数字。源静默是 `disconnected/source_silence`，自然租期到达是 `expired/candidate_expired`，报告中保留差异。
- `source_timeout_ms`（默认 1500ms）不会延长 `expires_ms`；浏览器显示租期取 `min(remaining_ms, lease_ms)`，`lease_ms` 最大 800ms，避免轮询中断留下旧卡。
- JSONL 是单一可信本机追加生产者合同：只读取启动后的完整新行，超长物理行会一直丢弃到该行的下一换行，尾部不得作为新事件解析。文件换 inode、缩短或最近已消费 4KiB 被改写会失败关闭。它不是任意同 inode 历史重写的完整证明；生产者不得 copy-truncate/重写已消费内容。
- 浏览器上报 `dom_updated` 与双 `requestAnimationFrame` 的 `paint_opportunity`，绑定 `session_id/revision/render_token/opportunity_id`。两条 POST 可乱序到达；服务器不以收达顺序判断真伪或时延，浏览器本地 `dom_to_raf_ms` 才描述二者间隔。浏览器 `performance.now()` 与服务端单调时钟不同域；报告里的“输入接收→绘制机会回报收达”只是本机传输/绘制机会，不能称为平板屏幕或玩家可见时延。

公开规格只使用虚构样例：例如回放来源的机器候选可以包含“计数候选”和“标签名称候选”，但比例、场景、规则和策略结论未知。它只能以 `source_kind=replay`、`evidence_status=machine_candidate` 的 `observation` 呈现，不能携带建议。真实回放绑定、来源哈希、帧号和结果只可留在项目外的私有材料中。

25B/25D 后续需给出同一 `case_id`/PTS/源序号、逐字段状态（含未知）、规则绑定和建议版本；任一关键条件缺失即撤回。25J 可消费上述视图字段做独立平板端原型，但平板送达、全屏遮挡、触摸及用户可见性都需另行验收。

新增验证：

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -m unittest discover -s src/jcc_human_coach/tests -v
node src/jcc_human_coach/tests/test_dynamic_browser.mjs
```

Python 覆盖 JSONL 未来追加、原始 `source_sequence` 不重编、源静默与自然过期的不同终态、历史浏览器回报不复活旧卡和回环报告。Chrome 用合成候选确认动态页面收到候选和 DOM 回报；页面在可得的两次 rAF 后上报绘制机会，未收到该回报必须保留为缺失结果。既有 `test_browser_visibility.mjs` 验证本地倒计时清卡。都不代表实机实时链路或平板可见性。
