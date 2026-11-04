# Android 平板短提示送达 v1（25J 可行性与接入合同）

## 结论与状态

目标是让**玩家自己操作**时，在 Android 平板的游戏画面上方看到一条短、非阻挡式的教练提示；这与 Mac 浏览器页面是两条不同的显示路径。当前仓库的 `jcc_human_coach` 只提供本机回放/本机 loopback 显示，不能证明平板看得到卡片。

本机起初没有 Java Runtime、Gradle、Android SDK 或 Android app 工程。25J 已将 Temurin JDK 17、Android command-line tools / Platform 35 / Build Tools 35 和 Gradle 8.10.2 安装到项目外、模式 0700 的私有构建目录；没有使用 sudo、没有修改全局 PATH 或系统设置。三个上游归档都以发布方 SHA-256 校验且归档测试通过，项目的 Gradle wrapper 又固定 Gradle 发布包 SHA-256，依赖解析记录在 `gradle/verification-metadata.xml`。

独立 `android/coach-overlay/` 当前包含合成演示和开发期 loopback 观察入口；构建与本机 JVM 测试是静态/离线证据，不能证明 `WindowManager.addView` 在任何设备、游戏全屏或厂商系统上实际可见。它没有安装、没有连接或唤醒平板。因此本文件和 APK 仍不是平板送达、可读性、遮挡、时延或实战可用的证据。

## 已核对的技术路径

API 26 及以上的标准路径是带 `SYSTEM_ALERT_WINDOW` 的 `TYPE_APPLICATION_OVERLAY`。Android 官方文档说明该窗口位于 activity 窗口之上、状态栏和输入法等关键系统窗口之下，并可能由系统调整位置、尺寸或可见性；它需要 `SYSTEM_ALERT_WINDOW`。API 23 起还必须由用户在系统界面单独授予“显示在其他应用上层”，应用只能通过 `ACTION_MANAGE_OVERLAY_PERMISSION` 引导，不能自行授予。

默认实现应是**只显示、不吃触摸**的 `WindowManager` 卡片：`FLAG_NOT_FOCUSABLE | FLAG_NOT_TOUCHABLE`，不含任何点击、坐标、无障碍服务、录屏或自动操作。候选初始布局是横屏 `TOP | CENTER_HORIZONTAL`、固定三行（标题、动作、理由）、短租期。三个文本字段分别限 18/28/32 个 Java 字符；任何一行超出 520dp 卡片可容宽度或长度限制时，整条提示不显示并记为本地失败，绝不静默截断关键条件。具体 y 偏移、宽度、字体、透明度和游戏 UI 遮挡必须在目标平板实测后才可固定。

卡片采用 `TYPE_APPLICATION_OVERLAY` 的 `params.alpha=0.70`。这低于 Android 文档描述的非受信任覆盖层触摸遮挡阈值 0.8，但 `FLAG_NOT_TOUCHABLE` 并不单独保证每个系统/厂商上都能触摸穿透；“非阻挡”不能据此宣称。即使不接收触摸，卡片仍可能遮住游戏信息，触摸穿透、实际可读性与视觉遮挡必须留待目标平板实测。

GitHub 调研优先参考了 [CyberTKR/Subly](https://github.com/CyberTKR/Subly) 的固定提交 `5471e01ceb93f383dd6899c88ea4213fe8b8dbd3`：其 MIT 许可的 `OverlayService` 展示 `WindowManager`、`TYPE_APPLICATION_OVERLAY`、`FLAG_NOT_TOUCHABLE`、前台服务和销毁时 `removeView` 的成熟显示生命周期。此项目尚未复制其代码、字幕/无障碍逻辑或资源；若未来复制任何部分，必须添加版权与 MIT 许可文本。其前台服务声明只是可参考的产品例子，不能不经目标 SDK、系统版本和商店/设备策略核对而照搬。

## 与玩家提示事件的边界

25J 只消费已经形成的短建议，不识别画面、不选阵容、不计算策略、不上传截图，也不注入任何游戏操作。与 `jcc_human_coach` 的对齐点为：

- 生产侧候选是同一流内严格递增的 `CoachEvent`：`stream_id`、`frame_id`、`sequence`、`frame_received_ms`、`emitted_ms`、`expires_ms`、`status` 以及可选短 `CoachAdvice`；
- 只有 `candidate_visible` 可以携带标题、动作短句和理由短句；`unknown`、`stale`、`conflict`、`scene_changed`、`disconnected`、`expired` 都必须清除旧卡；
- `source_kind`、帧身份、时钟、策略和版本均仍是上游声明，平板显示端不得把它们升级为真实画面、真实规则、最佳建议或可执行指令。

现有 HumanCoach 的 HTTP 服务绑定 `127.0.0.1`，只对 Mac 本机页面开放；平板不能通过该地址获得提示。因此 Android 接入前需要一个**独立、经审查的传输适配器**，而不是暴露现有 loopback 服务到局域网。

`jcc_human_coach` 已冻结 `human-coach-view-v1` 的 observation-only 表示。25J 的开发期适配器只接受该 schema，并原样保留其收缩后的显示边界：`stream_id`、`session_id`、`revision`、`render_token`、`status`、`visible`、`remaining_ms`、`lease_ms`、`source_sequence`、`source_kind`、`frame_id`、`context_version`、`evidence_id` 和短文本/空值。`case_id`、PTS 与逐字段 unknown 原因仍是上游审计字段，只能由上游私有记录透传；认证/策略/执行资格字段不能被平板显示端升级。

合成按钮仍只向 non-exported Service 发送动作，候选内容由 Service 本地构造。另有一个**只出站、不监听**的开发期 loopback reader：它从 Android 自己的 `127.0.0.1` 读取 `human-coach-view-v1`，再映射到不可扩展的 `tablet-overlay-envelope-v3`。两条模式互斥：启动观察先清卡、建立独立显示状态并使合成/旧观察 callback 失效；点击合成演示先停止观察。它不是局域网监听器、不是认证或配对方案，也尚未接入平板。适配器将审计字段保留在调用侧私有记录，只将下列受限字段映射到显示包，并生成自身严格递增的 `delivery_sequence`，例如：

```json
{
  "schema_version": "tablet-overlay-envelope-v3",
  "stream_id": "opaque-stream",
  "session_id": "opaque-session",
  "delivery_sequence": 18,
  "bridge_revision": 12,
  "source_revision": 12,
  "render_token": "render-12",
  "status": "candidate_visible",
  "visible": true,
  "remaining_ms": 700,
  "lease_ms": 700,
  "content_kind": "observation",
  "content": {
    "title_zh": "回放机器候选·未认证",
    "primary_zh": "对象：调用方声明的事实短句",
    "detail_zh": "未知：版本待核"
  }
}
```

平板使用 `min(remaining_ms, lease_ms)` 的正值作为本地收到消息后的显示租期，当前 `lease_ms` 最大为 800；它不是设备或主机间已同步的时钟。适配器应以新的全量快照续租，不能把 host 的单调毫秒直接当成 Android 时间。显示端只接受严格 JSON：双引号、无重复键、确切原始类型（字符串/布尔/整数）、ASCII `0-9` 数字及 ASCII `0-9/A-F/a-f` 的 `\\u` 转义；不使用 Android `JSONObject`/`JSONTokener` 的宽松单引号、等号/分号或类型强转语义。对于以下任一情况，客户端立即 `removeView`，取消旧计时器，且不得保留旧文案、数字或 action：解析/字段失败、未知 status、`visible=false`、显式撤回、租期到期、传输断开、同一会话 `delivery_sequence` 倒退/重复、revision 倒退、同 revision 更换 token 或语义文本、切换 stream/session。新 stream/session 到达时先清除旧流，再显示该条新卡。断连/超时只能在显示端本地撤回，不能伪造新的 `frame_id` 或 `source_sequence`。消息中不允许坐标、设备标识、原始画面、账号、自动执行字段或 HTML 富文本。

25J 不保存水印、撤销列表、窗口状态或任何 stream/session/token：每次 `CoachOverlayService` 新建均从空内存状态开始，退出、销毁或重建都会移除卡片。它不具备跨进程、跨重启或网络消息的重放防护，也不声称具备认证、配对或持久会话。

纯合成演示不在 Activity→Service 间传递候选内容、token 或任何跨组件状态：Activity 仅向 non-exported Service 发送 `SHOW_SYNTHETIC` 或 `CLEAR` 动作。Service 在同一主线程队列中按收到顺序自行构造固定、明确标为合成的 envelope；CLEAR 先到就清空，SHOW 先到就显示，服务重建/销毁一律从空内存状态开始。开发期 loopback 是另一条独立新连接：每次观察启动同时新建 bridge adapter 和显示状态，因此同一上游 session 的首个 delivery/frame 可以在重连后重新显示；前一连接的 generation 无法回流。两者均不具备认证、配对、跨重启重放防护或真实平板送达证据。

每次 SHOW/CLEAR 都递增内存 generation；过期 Runnable 捕获生成时的 generation，只有仍等于当前代次才允许调用过期撤回。因此 CLEAR→SHOW 后迟到的旧过期回调不会撤回新卡。显示计时以 `elapsedRealtime()` 判断过期，`Handler.postAtTime` 则换算为 `uptimeMillis()`；unknown/clear/断连或租期到期均在主线程立即移窗。服务以 `START_NOT_STICKY` 运行；`onTaskRemoved` 清卡后以最近 `startId` 调用 `stopSelfResult`，且仅当它返回 true 才移除前台状态，避免旧任务移除错误终止仍有更新启动的服务。调度与实际屏幕可见性尚未在 Android 设备验证。

这份 envelope 只规定显示端失效语义；认证、配对、加密、USB 反向转发的实际可用性和用户可见送达尚未验证，不能把它当作已具备安全的 Mac→平板传输。

## 当前最小 Android 原型

25O 已选择性接入为**开发期 loopback 观察入口**：只有 Activity 内的显式按钮动作触发后，Android 才以 `GET http://127.0.0.1:<port>/api/view` 轮询；它不监听端口，预期未来由独立会话配置 USB `adb reverse`。默认端口是当前未监听的 `18181`，只是便捷默认而非占用保证；Activity 仅提供 ASCII 整数 `1024–65535` 的本地输入，**不**接受外部 Intent 的自动启动或端口参数。后续若由项目工具/ADB操作，仍须在默认设备的该界面上显式填写并点击，不能把普通 Intent 当作 ADB 专用能力。host、scheme、path 固定为 `127.0.0.1`、HTTP、`/api/view`；reader 不扫描、探测或停止其他端口/服务，USB reverse 必须使用同一个明确端口。请求仅接受 `human-coach-view-v1` 且 `delivery=loopback_browser_only`、`tablet_delivery_verified=false` 的 machine observation，拒绝 advice 与任何认证/策略/执行资格声明。

每次读取按本地 RTT 扣减 lease；合法 unknown/等待态立即清卡但继续轮询。畸形 JSON 或字段错误也会本地生成 unknown、清除旧卡并继续按固定周期读取，不能遗留旧提示；HTTP 状态/连接错误、显式停止、服务销毁或容量 terminal 则清卡并停止本 reader。单次请求配置 250ms connect/read timeout、4096-byte上限，并由独立 deadline executor 在从连接登记起约 500ms 后主动 `disconnect()` 当前连接；响应头返回后、正文 EOF 后和输出候选前均复查单调期限及 generation。停止路径在 Service 主线程先失效 generation 并移除卡片；可能阻塞的 `disconnect()` 只由该 reader 独立的后台 close executor 执行，主线程不等待。某些 HTTP 栈仍可能让已登记的旧请求随后发出至多一次本机 GET，故不承诺“stop 后绝无请求”。旧 reader 使用独立可回收的执行资源，不能占住新 reader；最多追踪两个**尚未实际终止**的退休 reader，周期性非阻塞地请求 shutdown，并且只有 poll、deadline 和 close 三个 executor 都报告 `isTerminated()` 后才释放一个容量名额。这个池为**进程级且线程安全**：Service 销毁/重建不会清空仍活着的旧 reader 计数，新实例仍会在满额时 fail-closed 清卡而不创建新 reader。销毁实例同样只请求 shutdown，不能把 JVM 的中断请求当作已终止证据。此机制在本机受控慢头/慢体 JVM 测试中会 fail-closed，但不能保证 Android 网络栈对 `disconnect()` 的精确中断时间，也不是端到端时延承诺；Android/平板实测仍待完成。本入口只做本机合成 HTTP 联测，尚未连接真实平板或 HumanCoach 生产服务，不能证明 USB、平板显示、时延或送达。

可见三行中的第一行明确显示调用方声明来源而非升级信任：`合成演示·未认证`、`历史回放·未认证`、`回放机器候选·未认证` 或 `实时机器候选·未认证`；第二行同时显示 observation 对象和事实。若两者不能完整装入限制行则返回 unknown、绝不截断对象。所有四种都是 observation，不显示 advice；`evidence_status=independently_reviewed` 也不会使平板端称来源已认证。应用有两个不同动作：**撤回提示**只清除当前卡、若正在观察则仍继续轮询；**停止观察并撤回**会清卡、停止 reader 并使迟到 callback 失效。

独立 `android/coach-overlay/` Gradle 工程当前只包含：

1. 一个设置 Activity：回读 `Settings.canDrawOverlays` 的权限能力，并明确本原型不保存窗口或会话状态；它不读取游戏内容，只有用户点选后才可跳转系统悬浮窗授权页。其 loopback 控件仅接受受限端口，不能传入 host 或 URL，也不接受外部 Intent 自动启动。
2. 一个可就绪的前台 `OverlayService`：权限不足、文本无法完整绘制或 `WindowManager.addView` 失败时清状态、移窗；应用任务移除时才停止服务。它不记录 `window_added` 或跨实例状态。
3. 一个 `WindowManager` 渲染器：三行纯文本 `TextView`/受限布局，无网络图片、无坐标动作、`FLAG_NOT_TOUCHABLE`。
4. 两个受控 Activity→Service 入口：本地合成演示，以及可显式停止的 loopback 观察。JVM 合同测试覆盖“可见 → unknown/非法 → expired”、原始 JSON 类型/重复键/溢出/非 ASCII 数字、同 revision 语义不可变、来源标签与对象、CLEAR→SHOW 后旧过期 callback 不得撤回新卡、模式切换后旧 loopback generation 不得显示、同源同帧重连，以及停止在连接登记前、慢响应头、慢响应体、晚 EOF 与旧慢头不阻塞新独立 reader 的 fail-closed 本机 loopback 行为。退休资源池测试另外覆盖反复 stop 后达到容量、`shutdownNow()` 未终止时仍占用名额、实际终止后才恢复容量，以及 Service 销毁/重建后旧活 reader 仍占用同一进程级容量。测试 server 显式绑定 `127.0.0.1`，不监听局域网。它们不启动真实 Android Service、USB reverse、真实平板或真实 HumanCoach 生产服务；真实 Android 网络、覆盖层、断连探测和 25E 生产接入另行实机评审。

是否需要前台服务、通知渠道、Android 版本对应前台服务类型和通知权限，必须随最终 `targetSdk`、Android 版本和厂商策略再次核对。不要把 Subly 的 `specialUse` 声明直接复制到教练应用。

在已配置 Android SDK 的隔离环境中，可运行：

```sh
./gradlew --offline --dependency-verification=strict \
  testDebugUnitTest assembleDebug lintDebug
```

本轮将重新在隔离环境以 `--offline --dependency-verification=strict` 运行 `testDebugUnitTest`、`assembleDebug` 和 `lintDebug`；JVM 测试只能证明自定义严格解析器、内存状态机及确定顺序反例，并不执行 Android 内置 JSON 解析器、真实 Service 重建、真实覆盖层或屏幕可见性。`aapt dump badging` 静态回读仅确认包为 `com.android.jcc.coachoverlay`、`minSdk=26`、`targetSdk=35` 和四个所声明权限；它不等于设备安装、授权或覆盖层出现。

## 构建、权限与实机验证前置

总控在安排后续会话前，需要先满足并分别记录下列状态：

| 前置 | 当前状态 | 后续最小动作 | 不能据此推断 |
| --- | --- | --- | --- |
| Android 构建 | 隔离 JDK/SDK/Gradle 与 debug APK 已本机验证 | 固定 SDK/AGP/Gradle 校验记录，独立复核源码和 debug APK；后续只由总控串行安装 | APK 已在平板运行 |
| 平板权限 | 未查询、未请求 | 安装测试 APK 后由用户在系统“显示在其他应用上层”页面授予；应用以 `Settings.canDrawOverlays` 回读 | 游戏上方实际可见 |
| 前台存活 | 仅离线构建存在，未实机 | 在目标 Android/厂商上观察服务/通知/省电限制后的保持与停止 | 不被系统回收或无延迟 |
| 消息链路 | Mac loopback 不能到平板 | 设计并独立审查配对、认证、最小消息与断连；先用合成消息 | 真实策略已送达 |
| 游戏内体验 | 未测试 | 在空闲和真实全屏画面分别测试横竖屏、刘海/状态栏、遮挡、字号、触摸穿透、撤回 | 玩家已阅读、不会遮挡、提高胜率 |

标准 Android 路径仍是首次安装后由系统悬浮窗页面授予。安装包存在且平板重新接入后，总控可单独评估 ADB `appops` 对该设备/厂商的可用性并做回读；当前既没有安装包上机也没有连设备，因此不能预设它可行或不可行，更不能把它当作绕过平台权限的承诺。若系统要求，还需分别处理通知/电池限制页面。未满足系统前置时，应用必须保持不显示。

实机验收日志要把“Android service 接到消息”“`WindowManager.addView` 成功”“屏幕录像/人工看到提示”“玩家阅读并采取动作”分开记录，并将最后两项标为尚未验证，直到有独立实机证据。任何游戏画面、设备标识、账号、局域网地址和原始日志必须留在项目外私有目录，不提交公开仓库。
