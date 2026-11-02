# 第三方项目说明

除表中明确注明的代码片段外，本仓库不包含下列项目的源码或模型；安装脚本或用户按需从各项目的公开地址分别获取，并保留其原始版权及许可证文件。

| 项目 | 作者 / 维护者 | 许可 | 本项目中的使用 | 固定版本 |
| --- | --- | --- | --- | --- |
| [Sunflower](https://github.com/Carey8175/Sunflower) | Wang Wenjie | [MIT](https://github.com/Carey8175/Sunflower/blob/c4d6b3bc7dce2108dc79c01e57e71c740ae9fa71/LICENSE) | 早期原型调用 `SunflowerCatcher` 的画面和文字读取接口；当前观察路径直接调用 Apple Vision，保留兼容的只读画面和 OCR 适配代码。没有采用其未经校准的操作坐标。 | `c4d6b3bc7dce2108dc79c01e57e71c740ae9fa71` |
| [jcc-replay-analyst](https://github.com/HuanNan520/jcc-replay-analyst) | Xu Yunpeng（HuanNan） | [MIT](https://github.com/HuanNan520/jcc-replay-analyst/blob/0ab03d5053ae949aa8ee25b22ab156a6c8c2c432/LICENSE) | 调用 `FrameMonitor` 做通用画面变化检测，并在合成局面中通过 `DecisionLLM` 验证本地模型调用；原版完整实时流程尚未迁移。 | `0ab03d5053ae949aa8ee25b22ab156a6c8c2c432` |
| [scrcpy](https://github.com/Genymobile/scrcpy) | Genymobile 及贡献者 | [Apache-2.0](https://github.com/Genymobile/scrcpy/blob/v5.0.1/LICENSE) | 获取 USB 平板画面；安装脚本下载官方 macOS ARM 发行包并检查 SHA-256。 | `v5.0.1` |
| [Qwen3-VL](https://github.com/QwenLM/Qwen3-VL) | Qwen 团队 | [Apache-2.0](https://github.com/QwenLM/Qwen3-VL/blob/main/LICENSE) | 可选的本机场景识别实验，通过本机 Ollama 调用；模型需另行下载，不包含在仓库中。 | `qwen3-vl:4b-instruct` |
| [tft-advisor](https://github.com/ndh0408/tft-advisor) | ndh0408 | [MIT](https://github.com/ndh0408/tft-advisor/blob/8380b6dbeffe9edade7b198d8063bc3e18917b6d/LICENSE) | 装备配方候选实验直接包含 `craftableFrom` 函数片段及其原始许可；不包含上游游戏数据或素材。 | `8380b6dbeffe9edade7b198d8063bc3e18917b6d` |
| [RapidOCR](https://github.com/RapidAI/RapidOCR) | RapidAI 及贡献者 | [Apache-2.0](https://github.com/RapidAI/RapidOCR/blob/bd941dae09da793e76c6ba15c7aaf40f8424232a/LICENSE) | 可选离线文字转写实验调用已固定的官方发行包；不包含上游源码、依赖包或模型。模型来源及单独许可见 [RapidOCR 模型许可说明](https://github.com/RapidAI/RapidOCR/blob/bd941dae09da793e76c6ba15c7aaf40f8424232a/python/MODEL_LICENSES.md)。 | `v3.10.0` / `bd941dae09da793e76c6ba15c7aaf40f8424232a` |

Python 依赖及其传递依赖各自适用对应的软件许可证；版本资料与游戏内容并未包含在本仓库中。
