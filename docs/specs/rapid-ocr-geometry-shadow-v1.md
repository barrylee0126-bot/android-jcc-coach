# RapidOCR 几何影子候选 v1

实现：[`src/jcc_rapid_ocr_geometry`](../../src/jcc_rapid_ocr_geometry/)。这是对 RapidOCR 全检测输出的本机离线归一化层，不启动设备、不读取账号、不产生游戏建议或操作。

## 范围

既有 `jcc_rapid_ocr_candidate` 固定为识别-only，因此不返回检测框。本模块接收调用方已序列化的 `RapidOCROutput`：`txts`、`scores` 与 `boxes` 必须等长，且在这个窄裁区实验中必须恰好一项。多项不挑选高分项，直接保留 `unknown/ambiguous_detection_count`。

检测框的坐标只属于调用方声明的裁区。`result_type="RapidOCROutput"` 也是调用方传入的字符串，适配器不能据此认证后端身份，也不能认证该结果属于某个原始帧；输出相应固定为 `backend_identity_authenticated=false` 和 `input_frame_binding_authenticated=false`。四个点必须有限、非退化、非自交、相邻三点不共线、落在裁区内，并且每个点离裁区边缘至少一像素；任一点距边缘不足一像素返回 `unknown/text_box_touches_crop_boundary`。贴边框可作为诊断材料，但不能成为影子候选。适配器不另外声称或验证检测器多边形是否凸；只要满足这些固定的结构检查，形状仍只是调用方声明裁区内的机器框。

## 证据边界

即使输出 `development_candidate`，也只表示“该裁区里有一个内部检测框和对应机器转写”。它不证明完整原图中的区域位置、字段身份、数字可见性、棋盘归属、游戏阶段、人工真值或跨局准确率。输出恒定为：

- `eligible_for_geometry_gate=false`
- `eligible_for_policy=false`
- `eligible_for_execution=false`
- `accuracy=null`

因此模块可用于把检测框、边界失败原因和原转写放到私有逐帧对照中，但不能放宽现有未知门控，也不能直接触发提示、买卖或平板操作。

## 验证

运行：

```sh
PYTHONPATH=src python -m unittest src.jcc_rapid_ocr_geometry.tests.test_contract
```

测试只使用合成结构，不带 RapidOCR、模型或私人图像。真实录像对照应保存在仓库外私有目录，并明确标为机器候选而非准确率。
