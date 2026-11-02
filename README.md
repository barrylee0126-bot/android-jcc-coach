# 安卓平板金铲铲教练

面向 Android 平板《金铲铲之战》的开源教练项目。目标是让程序理解平板画面，结合版本信息及时给出对局建议，并逐步评估在用户授权下完成操作的能力。

**项目处于早期验证阶段。** 目前已经打通 USB 平板的只读连续画面、文字识别，以及画面变化检测；尚不能可靠理解整局、提供经过验证的实时建议、自动匹配或自主上分。

## 当前能力

- 使用 scrcpy 连续读取指定平板的画面；观察模式不发送点击，也不会进入匹配。
- 使用 macOS 的 Apple Vision 进行本地中文文字识别，通过适配层接入 Sunflower 的读取接口。
- 使用 jcc-replay-analyst 的画面变化检测模块处理实际画面。游戏内各区域还没有针对设备和版本完成校准。
- 画面仅在本机处理，当前观察命令不保存游戏截图。输出是识别数量和测速信息，不包含识别到的文字。

在一台 Apple Silicon Mac 与 USB 安卓平板的测试中，观察模块开始运行后首次画面约 3–5 秒；按每秒 4 帧配置采样时收到了连续画面，首次文字识别及取帧约 1.6 秒。另一次包含模块加载、6 帧采样、文字识别和退出的完整命令耗时约 13 秒。这些数据仅代表单次测试的采集与识别情况，不能代表完整决策延迟、识别准确率或对局表现。

## 安装与只读测试

目前提供的安装脚本仅支持 **Apple Silicon macOS**；需要 Python 3.11、Git、curl、Swift 编译器和 ffmpeg。平板需要开启 USB 调试并已授权这台电脑。请自行安装缺少的系统依赖，脚本不会升级系统现有的软件包。

```sh
git clone https://github.com/barrylee0126-bot/android-jcc-coach.git
cd android-jcc-coach
./tools/setup-macos
```

脚本会在项目目录中下载并校验官方 scrcpy、检出下方两个项目的固定版本、建立 Python 环境并编译本地识别程序。已有的对应版本会复用。第一次安装需要网络和数 GB 可用空间。

运行以下命令查看已连接设备，并将得到的设备标识填入配置文件：

```sh
./third_party/scrcpy-macos-aarch64-v5.0.1/adb devices
cp 设备配置.env.example 设备配置.env
# 编辑 设备配置.env，设置 ANDROID_SERIAL
./tools/jcc-observe
```

`tools/jcc-observe` 只读画面并输出 JSON 测试结果；它不会自动启动游戏、触摸屏幕或匹配。设备配置文件、截图、版本数据、模型和依赖目录不会提交到仓库。

## 可选：本地视觉模型实验

可以另外安装并启动 [Ollama](https://ollama.com/)，然后下载 Qwen3-VL 指令版，实验性判断当前画面属于大厅、模式选择、对局、加载、结算、历史记录或未知场景：

```sh
ollama pull qwen3-vl:4b-instruct
./tools/jcc-scene
./tools/jcc-decision-demo
```

模型约 3.3 GB，首次运行还需要加载模型；画面只发送给本机 `127.0.0.1` 的 Ollama 服务。场景实验仅输出类别及模型自评的置信度，后者尚未校准。决策演示使用合成局面，调用 jcc-replay-analyst 的决策模块；其提示词针对旧赛季，也没有接入真实游戏状态。

单次测试中，模型正确识别了一张非游戏测试图和一张游戏历史记录图；已加载模型时，历史记录图的模型处理约 4.4 秒，包含取帧与启动的完整命令约 12 秒。这还不是对局测试，不要据此自动操作或判断实时教练效果。

## 鸣谢与引用

本项目的早期实现直接调用了以下两个开源项目的模块：

1. [Sunflower](https://github.com/Carey8175/Sunflower)，作者 Wang Wenjie，MIT 许可证。用于对接画面读取和文字识别接口；其原有模拟器连接和画面坐标未直接用于本项目的平板操作。
2. [jcc-replay-analyst](https://github.com/HuanNan520/jcc-replay-analyst)，作者 Xu Yunpeng（HuanNan），MIT 许可证。使用其画面变化检测模块；原版实时教练面向 Windows 模拟器，还需要针对本项目适配。

画面传输使用 [Genymobile/scrcpy](https://github.com/Genymobile/scrcpy)，Apache License 2.0；可选场景实验使用 [Qwen3-VL](https://github.com/QwenLM/Qwen3-VL)。固定版本、许可证与使用范围见 [第三方项目说明](THIRD_PARTY_NOTICES.md)。这些依赖由安装脚本或用户按需单独获取，本仓库不重新分发其源码、模型或游戏素材。

## 许可证

本仓库原创代码采用 [MIT 许可证](LICENSE)。各第三方依赖保留各自的版权和许可证；《金铲铲之战》及相关名称和素材属于其各自权利人，本项目不是官方产品。
