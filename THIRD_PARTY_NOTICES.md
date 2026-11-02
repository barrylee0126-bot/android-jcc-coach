# 第三方项目说明

本仓库不包含下列项目的源码；安装脚本从各项目的公开地址分别获取，并保留其原始版权及许可证文件。

| 项目 | 作者 / 维护者 | 许可 | 本项目中的使用 | 固定版本 |
| --- | --- | --- | --- | --- |
| [Sunflower](https://github.com/Carey8175/Sunflower) | Wang Wenjie | [MIT](https://github.com/Carey8175/Sunflower/blob/c4d6b3bc7dce2108dc79c01e57e71c740ae9fa71/LICENSE) | 调用 `SunflowerCatcher` 的画面和文字读取接口，并注入本机只读画面与 Apple Vision 识别适配器。没有采用其未经校准的操作坐标。 | `c4d6b3bc7dce2108dc79c01e57e71c740ae9fa71` |
| [jcc-replay-analyst](https://github.com/HuanNan520/jcc-replay-analyst) | Xu Yunpeng（HuanNan） | [MIT](https://github.com/HuanNan520/jcc-replay-analyst/blob/0ab03d5053ae949aa8ee25b22ab156a6c8c2c432/LICENSE) | 调用 `FrameMonitor` 做通用画面变化检测；原版实时教练的 Windows / 模拟器依赖、决策和识别能力尚未完整迁移。 | `0ab03d5053ae949aa8ee25b22ab156a6c8c2c432` |
| [scrcpy](https://github.com/Genymobile/scrcpy) | Genymobile 及贡献者 | [Apache-2.0](https://github.com/Genymobile/scrcpy/blob/v5.0.1/LICENSE) | 获取 USB 平板画面；安装脚本下载官方 macOS ARM 发行包并检查 SHA-256。 | `v5.0.1` |

Python 依赖及其传递依赖各自适用对应的软件许可证；版本资料与游戏内容并未包含在本仓库中。
