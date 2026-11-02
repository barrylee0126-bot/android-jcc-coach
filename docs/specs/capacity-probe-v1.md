# Capacity probe v1：离线上场容量显示探针

实现：`src/jcc_capacity_probe/`。本模块只返回 `development_candidate` 或 `unknown`，读取显式给定的离线图像，复用现有 Apple Vision。没有设备入口，不发布 game-state-v1，不修改样本标签、复核状态或策略，不产生自动上场动作。

它解决一个很小的问题：在已经声明布局及显示区域身份的同一帧中，尝试读取完整的 `n/c` 显示；两个数都满足候选规则时，计算 `c-n`。这里的“人数”是**显示的已用容量候选**，不是通过检测棋子得到的实际棋子数量。显示语义、单位占用格数和版本规则均未核实。

## 1. 可导入接口

```python
from jcc_capacity_probe import (
    ProbeLayout, FrameEvidence, CapacityResult,
    parse_capacity_ratio, probe_capacity,
)

# 以下尺寸、区域及身份均为虚构示例，不是实机配置。
layout = ProbeLayout(
    layout_id="synthetic-example",
    image_size=(200, 100),
    roi=(20, 10, 160, 80),
    source_kind="synthetic",
    scales=(1, 2),
    minimum_raw_score=0.5,
)
```

`probe_capacity(image_bytes, *, source_kind, layout=None, evidence=None, ocr=None, expected_sha256=None)` 返回 `CapacityResult`。`ocr` 只需提供 `recognize(bytes) -> list[dict]`；真实运行使用 `jcc_device.vision_ocr.VisionOCR`，测试使用假 OCR。API 不启动 worker，调用方负责生命周期。

`FrameEvidence` 必须明确提供 `encoded_sha256/layout_id/source_kind/image_size/rotation_applied_degrees/roi`。`roi` 是**完整 OCR 输入裁切区域**，与 `ProbeLayout.roi` 使用相同整帧坐标，必须逐坐标精确相等；同一源帧、同一 `layout_id` 也不能把 A 区域的可见性/动画声明用于 B 区域。不匹配在任何 OCR 调用前返回 `frame_evidence_roi_mismatch`，整体三项数值为 `null`。缺少 `roi` 的旧构造/配置直接拒绝，不从布局自动补填。只有与本帧、布局和完整输入 ROI 一致的声明才可能继续处理：

| 声明 | 接纳候选所需值 | 其他值 |
| --- | --- | --- |
| `layout_matches` | `True` | `False` → `layout_mismatch`；默认 `None` → `layout_identity_unknown` |
| `visibility` | `clear_candidate` | `occluded` → `occluded`；默认 `unknown` → `visibility_unknown` |
| `animation` | `not_observed_candidate` | `observed` → `animation_observed`；默认 `unknown` → `animation_unknown` |
| `assessment_method` | 合成来源为 `synthetic`；回放为 `agent_development_observation` | 来源与声明不符 → `evidence_authority_mismatch` |

上述值是**调用方对容量 ROI 的开发观察声明**，代码不具备自动遮挡、动画或布局分类器，也不能认证声明正确。`clear_candidate/not_observed_candidate` 仅表示单张静态图上没有观察到容量区域受阻或动画；不能证明时间稳定。实际看到动画、遮挡或无法判断时必须使用相应拒绝/未知值，不能因为 OCR 读出了数字就填入“清晰”。真实声明不会升级成人工标注或独立复核。

阶段、画面主体、备战席可用棋子始终未知，与是否读取容量显示无关。这个探针不能建立“己方备战”上下文。

## 2. 结构化结果与源帧匹配

`result.to_dict()` 提供以下稳定字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | `capacity-probe-v1` |
| `implementation_revision` | 本次修复为 `roi-bound-sanitized-variants-v2`；整体 CLI 报告及逐帧结果均包含 |
| `source_frame.anonymous_ref` | `sha256:` 加本次输入图像完整编码字节的 SHA-256；没有玩家或设备标识 |
| `source_frame.encoded_sha256` | 便于和调用方同一源文件的字节摘要精确匹配 |
| `source_frame.source_kind/image_size/rotation_applied_degrees` | 显式来源类型、解码尺寸和已应用旋转声明 |
| `layout_id/input_roi` | 私有配置身份和全部 OCR 输入裁切范围 |
| `status` | `development_candidate` 或 `unknown` |
| `displayed_deployed/capacity/empty_slots` | 只有整体候选成功才为整数；未知时三项全部为 `null` |
| `reason_codes` | 固定拒绝原因码，不包含 OCR 原文或异常消息 |
| `confidence.calibrated_probability` | 固定 `null`，没有真实准确率校准 |
| `confidence.variants` | 倍率、原始 OCR 分数、文字框、状态、原因和处理耗时；不导出各倍率备选数值。整体未知时所有倍率诊断状态均为 `unknown`，原本成功的倍率标记 `discarded_due_to_unknown_result` |
| `context` | `phase/subject/deployable_bench_unit=unknown`；`temporal_stability=unverified_single_frame`；复核状态始终是未复核机器/Agent 候选 |
| `conditional_hint` | 仅描述本帧候选及条件；未知时不猜空位 |
| `automatic_action/accuracy` | 固定 `null` |

完整编码字节相同才得到同一个匿名引用；重新压缩/编码的图片会不同。该引用用于离线源图匹配，**不是时间戳、原录制帧号或连续性证明**。相同图片可在不同时间出现，不能仅凭哈希认定同一时刻或沿用旧帧结果。上游若使用自己的帧身份，应在核对输入摘要后自行绑定；本模块没有 TTL、新鲜度、当前帧水位或建议可见时间机制。

`CapacityResult.variants` 中存放的是经过裁减的 `VariantDiagnostic`，而非内部识别对象；公开属性中没有 `pair` 或其他倍率数值答案，`repr()`、`dataclasses.asdict()` 和 `to_dict()` 都不会留出被整体拒绝的备选值。内部比较所用 `_Variant` 不进入结果对象。唯一可消费的数值是整体成功时的 `displayed_deployed/capacity/empty_slots`。

虚构 `2/3` 成功时，`empty_slots=1`，提示为：

> 本帧候选空位为1；若为己方备战且备战席有可用棋子，可检查空位。阶段、主体、备战席是否有可上场棋子均尚未证实。

明确读到虚构 `3/3` 时才会产生本帧 `empty_slots=0` 的候选。没有显示、未提供下一帧、识别失败或被拒绝时均为未知，绝不以 0、满编、上一帧或等级补值。本次四张开发图没有建立真实“有空位”正例；正空位提示只在合成测试中验证。

## 3. 图像和数值规则

- 输入只允许 PNG/JPEG 完整单帧，最大 25 MiB、1600 万解码像素；EXIF 方向必须为空或 1。不会自动旋转、重采样整幅画面或套用其他分辨率。
- 原图尺寸须精确等于布局尺寸；布局来源类型和证据的旋转声明必须一致。相同尺寸不足以证明相同游戏布局，仍需要本帧的显式身份声明。
- ROI 为完整解码帧左上原点的 `(left, top, right, bottom)` 整数框，右/下排除。只裁切这一范围，不补边、锐化、修补、识别棋子或读取其他区域。
- 默认对同一裁切做 1 倍和 2 倍最近邻输入。配置要求至少两个不同的 1–4 整数倍率；每次放大输入同样限制为 1600 万像素。两倍率使用同一 Apple Vision，属于相关实验。
- 每倍率必须恰好一个有效文字框；无文字、多框、无效坐标/分数、文字框触及裁切边缘、低分或异常均拒绝，不拼框、不按最高分挑选。
- 语法只接受完整 ASCII `n/c`，可去掉两端 ASCII 空白；不接受内部空白、前导零、全角数字、负数、小数、额外标签、`O/l`、缺失分母或无限上限。
- 必须 `c>0`、`0<=n<=c`。每数最多 6 位仅为输入资源限制，不是从未知模式推导的游戏容量上限。不会由等级、模式印象、棋子外观或历史帧推断分母。
- 任一倍率未知 → `variant_incomplete`；两个比例不同 → `variant_disagreement`。均清空整体人数/容量/空位；多个拒绝原因去重保留。

`minimum_raw_score=0.5` 是未校准的工程过滤参数，不是“50% 正确率”。即使两个倍率都为 1.0 且同值，也可能同错，不能替代独立人工复核。文字框可反变换到整帧，但依赖范围仍是完整 `input_roi`；检查上下文所看过的其他区域由调用方负责，不应将文字框冒充整个依赖范围。

## 4. 本机可复现入口

在项目根目录：

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_capacity_probe/tests -v
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_capacity_probe --help
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m jcc_capacity_probe --config /absolute/private/config.json --report /absolute/private/results.json
```

前两条不启动设备或真实 OCR；最后一条只启动现有本机 Apple Vision worker，不连接平板、不扫描样本工作区、不下载依赖。真实实验使用的绝对路径、原图哈希、ROI、单帧 Agent 声明和报告只留在本机私有配置/报告中，不写入本规格或代码默认值。

私有配置形式：

```json
{
  "schema_version": "capacity-probe-v1",
  "layout": {
    "layout_id": "caller-selected-development-layout",
    "image_size": [200, 100],
    "roi": [20, 10, 160, 80],
    "source_kind": "replay",
    "rotation_applied_degrees": 0,
    "scales": [1, 2],
    "minimum_raw_score": 0.5
  },
  "frames": [
    {
      "image_path": "/absolute/private/image.png",
      "expected_sha256": "<64 lowercase hex digits of this exact image>",
      "evidence": {
        "encoded_sha256": "<same exact image digest>",
        "layout_id": "caller-selected-development-layout",
        "source_kind": "replay",
        "image_size": [200, 100],
        "rotation_applied_degrees": 0,
        "roi": [20, 10, 160, 80],
        "layout_matches": null,
        "visibility": "unknown",
        "animation": "unknown",
        "assessment_method": "agent_development_observation"
      }
    }
  ]
}
```

示例没有真实图片或可用证据；其占位摘要必须替换，未知证据将拒绝。`frames` 是显式哈希固定的文件白名单，不支持 glob、目录发现或自动追加样本。摘要不符返回 `source_hash_mismatch`，不读取新内容为候选。CLI 仅将真实输入路径用于读取，不在输出复制文件名、OCR 原文、玩家信息或异常文本。

报告必须是检出目录之外的 JSON，所在目录权限为 0700；原子写入文件权限为 0600，不允许覆盖配置或图像输入。CLI 同时打印结构化结果。`run_config(config_path, report_path=...)` 可供本机脚本导入；`report_path=None` 时只返回对象。

## 5. 验证范围和集成限制

合成测试验证完整比例、正空位条件提示、明确零空位、缺分母/无限上限/非法值、空白、多框、低分、坏框、倍率矛盾、已知遮挡/动画覆盖“完美 OCR”、未知证据、错帧/旋转/尺寸/来源、EXIF、损坏图片、异常脱敏、匿名哈希绑定和私有报告防覆盖。测试像素和 OCR 答案全部虚构，不能当作实机准确率。

补充回归覆盖同帧/同布局名移动、缩小或纵向移动 ROI 时旧证据必须拒绝且 OCR 调用次数为零；缺少/非法 ROI 的证据必须拒绝；倍率冲突及部分失败时，公开属性、`repr()`、`asdict()` 均不暴露 `pair`，各倍率只保留未知诊断。当前合计 32 项测试。

真实实验只涉及已暴露开发的 11B 四张图。原图及原始 ROI 保留私有目录，不更改标签、复核或分集；候选和 Agent 据图观察都不是人工真值。4 张图属于同一已知回放来源，不能由截图数、倍率或重复运行增加独立场次。没有读取其他样本组、操作平板或启动匹配。

处理耗时使用本机 `perf_counter`。单帧耗时包括摘要、图像解码、门控，以及执行时的裁切/编码/两倍率 OCR/解析；图像文件读取和 worker 创建另外记录。新 worker 的首请求可能含首次识别初始化，不能据此估计稳定 P95 或对局响应速度。不存在设备变化到提示或提示到动作的延迟测量。

总控可以按输入字节摘要关联候选到同一离线源帧，并展示上述条件提示。任何实战状态、逐帧上下文、主体验证、备战席检测、动作或策略集成都需要另外的证据和授权范围；本探针的候选本身不提供这些依据。

此次修复后的总控/17A 调用方应要求报告及逐帧的 `implementation_revision=roi-bound-sanitized-variants-v2`，核对私有配置摘要、源图摘要和 `input_roi`，再读取整体结果。没有该修订标记的旧私有结果应视为已被替代，不因源图和 `layout_id` 相同继续沿用。新私有结果的具体入口及文件摘要记录在本机修复说明中。
