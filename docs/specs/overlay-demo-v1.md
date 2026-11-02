# 短提示本机原型 v1

开发 14A，2026-11-02。它用于检查 Mac 投屏侧短提示的阅读、更新和撤回体验。

**所有内容均为“模拟演示”。没有接入真实局面或策略，没有读取游戏画面、操作平板、调用模型或访问公网。** “纸片小方”、条件、动作和有效期均为手写虚构示例；演示有效不代表实战建议有效。

## 1. 启动、查看、停止

在项目根目录执行：

```sh
tools/jcc-overlay-demo
```

浏览器打开终端显示的本机地址，默认是 `http://127.0.0.1:8765/`。端口被占用时运行：

```sh
tools/jcc-overlay-demo --port 0
```

需要启动时打开浏览器，可加 `--open`。默认不自动打开浏览器。回到启动它的终端按 `Ctrl+C` 停止；没有后台常驻进程、账号、自动下载或自启设置。

只使用 Python 标准库和随代码附带的网页文件。启动器优先使用项目 `.venv/bin/python`，不存在时使用 `python3`；不安装依赖。也可以运行：

```sh
PYTHONPATH=src python3 -B -m jcc_overlay --port 0
```

服务固定绑定 `127.0.0.1`，不提供监听公网的参数。无目录浏览、文件导入、设备入口或任意建议上传入口；页面只加载本机脚本、样式和接口。请求不写访问日志，不保存提示历史。

## 2. 阅读和演示方式

先选择“有效提示”，从上到下可扫读：

1. 常驻的“模拟演示”、当前状态和剩余秒数。
2. 动作对象“第 2 格 · 纸片小方”、大字“可考虑保留”。
3. 一行虚构理由，以及常驻的“虚构数据 · 不代表实战建议”。

“打开小窗”会打开紧凑页面，建议放到投屏旁边；也可直接在地址后加 `?compact=1`。默认请求约 460×270 的浏览器窗口，实际内容区域受浏览器窗口边框影响。页面已检查 460×270、360×240 两种内容区域；此示例没有溢出。动作字号分别为 28px、27px。点击“收起”只保留模拟标识、状态和声明，此例卡片高 79px。

这是普通浏览器小窗，不是系统级置顶或鼠标穿透窗口。没有自动调整投屏、移动其他窗口或申请屏幕权限；真实投屏旁的遮挡和阅读体验仍待用户实际验收。页面切到后台时会撤下提示，回到前台需要新的演示修订，不恢复旧动作。

| 状态按钮 | 行为 |
| --- | --- |
| 有效提示 | 新建一条 12 秒的虚构提示；反复读取不续期。 |
| 即将到期 | 新建一条 2.5 秒的虚构提示，黄色状态及倒计时；到期后撤下对象、动作和理由。 |
| 已失效 | 直接显示“提示已到期”，无旧对象或动作。 |
| 信息不足 | 显示“信息不足，暂不建议”，不猜测缺失输入。 |
| 信息冲突 | 显示“信息不一致，暂不建议”，撤下旧动作。 |
| 模拟断连 | 显示“连接中断，提示已撤下”，不沿用旧提示。 |
| 画面变化 | 显示“画面变化，提示已撤下”；这里只是合成事件，没有读取游戏画面。 |

“播放一轮”按一次时间线演示：0 秒有效、剩余 3 秒进入即将到期、8 秒到期、10 秒信息不足、13 秒信息冲突、16 秒模拟断连并结束。没有自动循环或续期；手动选择状态会取消这条时间线。

这些毫秒数只是让人看清状态的演示配置，不能作为真实游戏的有效期或时延目标。

## 3. 独立合成接口

代码入口：[src/jcc_overlay](../../src/jcc_overlay/)。本模块没有导入 `jcc_strategy`、`jcc_device`、`jcc_catalog` 或共享 Schema，不代表这些管线已经接通。

Python 接口为 `Advice / OverlayInput / OverlayStore`：

```python
from jcc_overlay import Advice, OverlayInput, OverlayStore

store = OverlayStore()
now = store.now()  # 本实例的当前单调时间，单位毫秒。
message = OverlayInput(
    sample_kind="synthetic",
    host_clock_id=store.host_clock_id,
    stream_id=store.stream_id,
    source_revision=1,
    context_epoch=1,
    status="valid",
    advice=Advice(
        advice_id="paper-example-1",
        target="第 2 格 · 纸片小方",
        action="可考虑保留",
        reason="虚构条件：已有同名纸片，留作配对。",
        created_ms=now,
        expires_ms=now + 8_000,
    ),
)
assert store.publish(message)
assert store.view()["simulation"] is True

# 一个新事件就是失效屏障；不保留旧动作。
assert store.publish(OverlayInput(
    sample_kind="synthetic",
    host_clock_id=store.host_clock_id,
    stream_id=store.stream_id,
    source_revision=2,
    context_epoch=2,
    status="unknown",
    advice=None,
))
assert store.view()["advice"] is None
```

`source_revision` 是本合成展示流的修订，不是已经接通的 `game-state-v1.snapshot_revision`；`context_epoch` 同样只是调用者提供的合成上下文。`host_clock_id` 和 `stream_id` 由本实例产生，只接受本实例的标识；给其他时钟改标签不构成同域证明。

| 输入内容 | 本版约束 |
| --- | --- |
| `sample_kind` | 只能是 `synthetic`；`live/replay` 一律拒绝，没有切换开关。 |
| `source_revision / context_epoch` | 修订严格递增，上下文不能倒退；布尔值不当作整数。 |
| `status` | `valid/unknown/conflict/disconnected/invalidated/expired`；`expiring` 从剩余时间派生。 |
| `advice` | 只允许有效状态携带；非有效状态必须为 `None`。 |
| 文案 | 对象最多 28 字符、动作 20 字符、理由 60 字符，无空白首尾或控制字符。实际小窗适配只验证了本交付的短文案，不承诺任意上限文案都能放进 360×240。 |
| 时间 | 有限数值、签发时间不在未来、截止晚于签发；演示时长至多 30 秒。`now >= expires_ms` 即到期。 |
| 身份 | 同一建议 ID 不可重发、改文案或延长后复用；失效后必须有新的修订和建议 ID。 |

错误输入立即清空旧提示，返回 `False`；时钟倒退或不可用后持续拒绝，须新建实例。输出为副本。上述检查只证明合成展示协议自洽，不能证明游戏输入、规则或来源正确。

本机 HTTP 接口：

- `GET /api/view`：当前展示快照，含 `schema_version="overlay-demo-v1"`、`sample_kind="synthetic"`、`simulation=true`、`execution_eligible=false`、会话、展示修订、状态、剩余毫秒、展示租期及对象/动作/理由。非有效状态的 `advice=null`，没有隐藏旧文案。
- `POST /api/demo`：只接收 `{"scenario":"valid"}` 等固定按钮名或 `play`，只产生内置虚构数据；请求需来自本机页面的同源地址。不接收局面、任意提示、文件或设备参数。

Python 接口和 HTTP 接口均不是跨进程签名或信任凭证。页面标签不会把输入升级为真实核验。

## 4. 撤回与未来接入条件

当前已实现的展示保护：

- 服务端每次读取都用当前单调时间复核截止时间，读取不续期；到期后清空建议。新合成状态、非法输入、冲突、断连或上下文倒退会撤下旧提示。
- 页面约每 300ms 读取本机状态，每 75ms 检查本地倒计时。浏览器不把服务端单调时间与自身时间直接相减，而是从请求开始计时，保守扣除整个请求耗时；同一修订的后续读取不能延长已有截止点。
- 单次请求超时 900ms；展示租期至多 1,200ms。读取失败或租期耗尽就清空旧动作。页面同时观察自身单调时间和墙钟经过量，发现倒退或恢复后已超时便撤回。
- 较旧响应不能覆盖较新展示修订；手动切换和页面可见性变化会建立请求屏障。断连、到期或后台切换后，同修订即使再次收到也不复活；新提示须有新的修订。

这些是定时器策略，**不是硬实时保证或实测延迟指标**。浏览器暂停、Mac 休眠、主线程阻塞时，程序无法保证在指定物理毫秒点改写屏幕；恢复执行后会重新检查和撤回。服务端也不会自行获知游戏变化。

后续接真实策略时，需要由总控另行审查适配器及生产接受策略。不能把真实数据改成 `synthetic`、复制时钟标签或修改本版拒绝条件来声称完成接入。至少需要：

1. 按 [strategy-v1](strategy-v1.md) 在每次签发、读取和刷新时调用对应会话的 `revalidate(advice)`；建议须能追溯规则、依据、流、来源帧、快照修订、上下文和模式/版本。`conditional_fixture` 只能继续标模拟，`refused/not_applicable` 不能携带动作。
2. 按 [game-state-v1](game-state-v1.md) 接入最新帧、失效屏障和依赖字段，未知、冲突、过期、遮挡、场景/模式/版本变化、任一新帧或新快照、依据撤销及断连都撤回旧建议。晚到识别或旧策略结果不得恢复它。
3. 在真正相同的单调时钟域内取策略、全部依赖字段、证据与展示租期截止时间的最小值；传输到浏览器时只给保守剩余预算，不能在接收、重试或重连时重置有效期。
4. 重连、流或时钟域改变时更换会话；证明实际事件通路能通知撤回，并覆盖丢包、晚到、睡眠恢复和多个窗口。再验证真实内容字号、遮挡、延迟和用户阅读，不用本演示成绩替代。

本交付没有实现上述真实适配器，没有对局建议质量、速度或上分能力结论。

## 5. 验证入口与证据

```sh
.venv/bin/python -B -m unittest discover -s tests -p 'test_overlay_*.py' -v
node --check src/jcc_overlay/web/overlay.js
```

Python 测试仅需标准库；`node --check` 为可选的本地脚本语法检查，程序运行不依赖 Node。2026-11-02 在项目 Python 3.11.16 下 26 项测试通过，也在本机 Python 3.14.7 以 `python3 -S -B -m unittest discover -s tests -p 'test_overlay_*.py' -q` 关闭第三方包加载后通过。覆盖精确到期、反复读取、撤回、旧修订/ID、未知/冲突/断连、异流异钟、时钟错误、真假布尔、文本约束、真实来源拒绝、合成时间线和本机 HTTP 边界。

本机 Ego 浏览器实际检查了有效、即将到期、自动到期、信息不足、冲突、断连、画面变化、收起/展开、小窗尺寸、读取故障及恢复；完整的一次播放按六种状态依次走完。模拟读取故障后旧动作未复活，新修订可重新显示。最后在有效提示显示期间用 `Ctrl+C` 实际停止服务，退出码为 0，小窗显示断连且旧对象/动作清空；验证服务和浏览器窗口均已关闭。这些是虚构输入的展示测试，不是游戏链路成绩。

- [完整页面](../../src/jcc_overlay/evidence/full-valid.png)、[有效小窗](../../src/jcc_overlay/evidence/compact-valid.png)、[即将到期](../../src/jcc_overlay/evidence/compact-expiring.png)、[已到期](../../src/jcc_overlay/evidence/compact-expired.png)、[信息不足](../../src/jcc_overlay/evidence/compact-unknown.png)。全部仅含原型页面，没有游戏截图或个人信息。
- [浏览器状态和布局记录](../../src/jcc_overlay/evidence/browser-checks.json)。截图与记录是本次演示的证据，不会由正常运行程序自动生成。

本次不修改 `.gitignore`、共享 Schema、README 或总控台账，不提交或推送。现有默认忽略规则仍忽略本任务启动器和 `tests/test_overlay_*.py`；总控审阅合并时需单独决定放行，不能仅看 `git status` 认为这些文件不存在。私有交接记录仍放在被忽略的 `资料/开发/14A_短提示原型.md`。
