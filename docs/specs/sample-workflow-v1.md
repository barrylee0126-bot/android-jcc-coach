# sample-workflow-v1：本地样本导入、标注与复核

开发11A，2026-11-02。代码入口：[`../../src/jcc_samples/`](../../src/jcc_samples/)。

本工作流接收调用方已有的本地图片，不获取平板画面、不联网、不执行识别。新样本的全部 8 个核心字段及 5 个商店格均为 `undetermined/unknown/null`；不会把未标注位置当成空格、零或正确答案。

**本轮真实对局样本、真实人工标注、真实验收场次均为 0；识别准确率不可测。** 合成像素只用于工具验证。单元测试中声明 `sample_kind="real"` 的对象也是虚构元数据，只验证真实记录的结构及分集拒绝行为。

## 1. 契约和私有存储

沿用 [`game-state-v1.md`](game-state-v1.md) 第 5 节、[`sample_annotation` Schema](../../schemas/game-state-v1.schema.json) 和现有 `validate_annotations()`，不改动共享契约。

调用方必须每次明确提供 `--private-dir`。工作区必须是**项目及任何 Git 检出目录之外的绝对私有目录**。初始化只创建新目录，父目录必须已存在；不会接管、清空或覆盖现有目录。目录权限为 `0700`，文件为 `0600`，再次读取时检查所属用户、权限及文件硬链接。

程序拒绝路径中的符号链接、`..`、项目目录、包含 `.git` 标记的目录，以及可识别的 iCloud、CloudStorage、Dropbox、OneDrive、Google Drive、Box、Synology Drive 路径。macOS 的 `/var`、`/tmp` 可能是符号链接；调用方应先取 `Path(...).resolve()` 的规范路径。工具不调用任何上传、同步或网络接口。任意第三方软件配置的自定义同步/备份目录无法通过路径名完整识别，调用方仍须选用未被同步的本地目录。

工作区结构：

```text
<explicit-private-dir>/
  .lock                     # 本机进程锁
  workspace.json            # 唯一可变真源：annotations 数组、私有资产索引、修订和操作记录
  assets/asset-<random>.png  # 原始字节副本；JPEG 使用 .jpg
  edits/<caller-name>.json   # 私有人工编辑模板
  manifests/<revision>.json  # 导出的 sample-annotation-v1 JSON 数组；拒绝覆盖
```

索引不额外记录输入文件名、源路径、设备标识、玩家昵称或 OCR 原文；图片仍按原字节私有保存，可能包含调用方尚未脱敏的画面或 EXIF。标注中的棋子名等答案只随私有标注保存在磁盘；CLI 不打印图片、标签值、操作者输入的代号或源路径，只返回计数、修订号、随机生成的样本代号、尺寸和清单 SHA-256。所有调用方提供的身份/分组代号使用 `[a-z][a-z0-9_-]{0,63}`，不得使用个人资料或设备序列号。命令参数错误也不回显输入内容。

`workspace.json` 中每个私有资产索引项记录：随机 `asset_ref`、私有相对路径、声明的样本类型、**导入后实际保存的原始图像字节 SHA-256**、尺寸、格式，以及宽高与 RGBA 解码像素的 SHA-256。后者用于发现换元数据、改文件名或无损重编码后的完全相同像素；不是近似去重方法。

- `sample_kind="synthetic"`：按共享 Schema，标注必须为 `quarantine`、`annotation_method="synthetic"`、`asset=null`。合成图像的尺寸和哈希由私有索引保存，不扩展 Schema，不把合成图片伪装成真实资产。
- `sample_kind="real"`：必须由调用方显式声明；标注包含 `local_only` 资产引用与字节哈希，初始为 `human/draft`。`human` 表示后续标注流程，不代表导入时已完成了人工答案。工具无法验证调用方关于真实来源、脱敏程度或场次独立性的声明。

支持单帧 PNG/JPEG，每张编码文件最多 32 MiB、解码尺寸最多 1600 万像素。检查文件签名、图片完整性及完整解码，不依赖扩展名；不支持动画、多帧图片及未应用的 EXIF 旋转。工具保持字节不变，不自动旋转、裁剪、脱敏或剥除 EXIF；如需处理，先在另一个私有副本中完成，再导入实际要标注的那份图片。

`frame.host_received_monotonic_ms` 是**这次本地导入完成解码附近的 Mac 单调时钟时间**，不是原录制的设备采集时间或实时收帧时间。每张导入图生成独立的导入流、时钟域与帧代号，序号和上下文代数从 0 开始；设备时间、视区和校准全部为空。本工具不能恢复原录制时间线，也不能用于证明在线新鲜度或链路时延。

## 2. 最小可复现运行

在项目根目录运行，使用现有 `.venv`；不安装新依赖：

```sh
cd /path/to/android-jcc-coach
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s src/jcc_samples/tests -v
```

下面的合成演示可直接执行。显式目录必须尚不存在，且父目录已存在、未被同步。演示会将生成的输入和导入副本都留在该私有目录，供本机检查；不会生成真实样本。

```sh
JCC_SAMPLE_DIR="$HOME/Library/Application Support/jcc-samples-demo-11a"
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python - "$JCC_SAMPLE_DIR" <<'PY'
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from PIL import Image

root = Path(sys.argv[1])
def cli(*args, expected=0):
    result = subprocess.run(
        [sys.executable, '-m', 'src.jcc_samples', '--private-dir', str(root), *args],
        capture_output=True, text=True,
    )
    assert result.returncode == expected, result.stderr
    value = json.loads(result.stdout if result.returncode == 0 else result.stderr)
    print(json.dumps(value, ensure_ascii=False))
    return value

cli('init', '--dataset-id', 'synthetic-demo-v1')
buffer = io.BytesIO()
Image.new('RGB', (64, 40), (30, 70, 110)).save(buffer, format='PNG')
with os.fdopen(os.open(root / 'synthetic-input.png', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as out:
    out.write(buffer.getvalue())
args = ('import', '--image', str(root / 'synthetic-input.png'), '--sample-kind', 'synthetic',
        '--recording-session-id', 'synthetic-session-a', '--dedupe-group-id', 'synthetic-group-a',
        '--state-group-id', 'synthetic-state-a', '--annotator-id', 'annotator-a')
sample = cli(*args)
cli('edit-template', '--sample-id', sample['sample_id'], '--out', 'edits/draft-1.json')
cli('annotate', '--edit-file', 'edits/draft-1.json', '--annotator-id', 'annotator-a')
cli('review', '--sample-id', sample['sample_id'], '--reviewer-id', 'reviewer-b',
    '--evidence-ref', 'synthetic-demo-review', '--expected-revision', '2')
cli('export-manifest', '--out', 'manifests/revision-3.json')
assert cli('validate', '--manifest', 'manifests/revision-3.json')['real_samples'] == 0
assert cli(*args, expected=2)['error']['code'] == 'duplicate_image'
assert cli('split', '--recording-session-id', 'synthetic-session-a', '--target', 'acceptance',
           '--expected-revision', '3', expected=2)['error']['code'] == 'synthetic_split_forbidden'
PY
```

演示中的 `annotate` 只应用未经改动的全未知模板；`review` 是合成流程测试，不代表真实人员检查了游戏画面。真实标注时须先在私有编辑器中按画面填写模板，再执行对应操作。

## 3. 真实样本的操作顺序

所有命令都在上述项目根目录执行，均需带 `--private-dir "$JCC_SAMPLE_DIR"`；这里的变量必须指向**新的真实工作区**，不可复用合成演示的 `dataset_id` 或场次。

1. `init --dataset-id <revision-code>` 创建工作区。`import --image <local-file> --sample-kind real --recording-session-id <session-code> --dedupe-group-id <group-code> --state-group-id <state-code> --annotator-id <person-code>` 导入调用方实际持有的图片。组号必须由录制来源及去重复核确定，不得用文件名变化制造新场次。导入返回随机 `sample_id`，所有标签未知。
2. `edit-template --sample-id <sample-code> --out edits/draft-1.json` 写出当前修订的完整标签。人工编辑该私有文件；保持文件权限 `0600`。文件只包含 `sample_id`、`expected_revision`、`labels` 和 `conditions`；不允许借此修改来源、样本类型、哈希或分集。然后执行 `annotate --edit-file edits/draft-1.json --annotator-id <person-code>`。任何一次标注都会重置为 `draft`，清空旧复核人，但不改变分集。
3. 不同人员检查当前图片及当前标签后，执行 `review --sample-id <sample-code> --reviewer-id <different-person-code> --evidence-ref <private-evidence-code> --expected-revision <current-revision>`。分歧解决使用 `--status adjudicated`；未解决目标保持未知。复核不生成/更改标签，也不改变分集。私有操作记录关联依据代号与当前标签哈希，但不能证明两个代号确实来自不同的人。
4. `split --recording-session-id <session-code> --target development|acceptance|quarantine --expected-revision <current-revision>` 显式分集；可重复指定场次。一次移动完整场次，并递归纳入共享去重组关联的其他场次。返回受影响场次/样本数，不自动复核或猜测答案。只有真实样本、已有独立复核及通过契约检查的整个关联组才能进入验收集。然后 `export-manifest --out manifests/<new-revision>.json` 冻结不可覆盖的清单，并记录返回的 SHA-256。

`validate` 可取得当前工作区修订号。标注模板携带其创建时的修订号；复核和分集要求显式提供当前修订号。工作区变化后，旧操作以 `revision_conflict` 拒绝，不覆盖较新结果；应重新阅读当前样本及修订后再制作/复核。工具对同一工作区使用非阻塞进程锁，忙时返回 `workspace_busy`。

验收样本禁止直接改标注。需先将整个关联场次移回 `quarantine`，完成修改、重新复核和分集，再导出新清单。已导出的历史清单不会随工作区变化而重写；冻结评测时还应保存私有工作区及复核依据，并以新的 `dataset_id` 管理后续修改的验收版本。不能用当前工作区的 `revision` 冒充历史导出时的修订号。

已分到开发/验收集的场次不能直接再导入一张隔离样本，因为会违反场次隔离；应先把完整关联组移回隔离区。每个去重组在验收清单内只能有一个代表帧；存在多帧时会拒绝进入验收。本版不自动挑代表帧、不删除样本，也不自动拆组；可另建新的冻结工作区，仅导入已复核的代表帧并保留原场次和组号。近似画面的组号仍需按原契约人工核验。

## 4. 标签编辑规则

模板必须保留全部核心字段及按 1–5 排列的商店。字段具体值类型见共享 Schema。

```json
{
  "applicability": "undetermined",
  "truth_status": "unknown",
  "value": null,
  "reason": "not_annotated",
  "region": null
}
```

按真实画面判断：

- 是否适用还不清楚：保持 `undetermined/unknown/null`，写明原因；适用但遮挡不可辨：`applicable/unknown/null`，如 `reason="occluded"`。
- 已知答案：`applicable/known/<typed-value>`、`reason=null`；已知商店格包括空格在内必须有本帧的归一化正面积 `region`。`{"kind":"empty"}` 只能由人工确证，不能作为默认值。
- 确实不适用：`not_applicable/unknown/null`、`reason="not_applicable"`。不能为了提高准确率将困难字段改成不适用；游戏版本答案仍需契约规定的 `verification_ref`。

本工作流不调用 OCR/模型，不为未知补答案，也不计算正确率。CLI 中 `accuracy` 始终为 `null`；`real_samples` 等是声明类型的计数，不是来源或准确率认证。

## 5. 校验范围和清楚失败

`validate` 先检查工作区，再对全部标注调用原有 `validate_annotations()`，并实际读取资产检查尺寸、格式、字节与像素哈希及私有映射。`validate --manifest manifests/<file>.json` 对导出的 JSON 数组执行同样检查。清单内样本必须来自同一私有工作区；工具不会按清单提供的任意路径读取文件。

Python 接口：

```python
from src.jcc_samples import SampleStore, read_annotation_manifest

store = SampleStore("/absolute/private/output")
summary = store.validate("manifests/frozen-1.json")
records = read_annotation_manifest("/absolute/private/output/manifests/frozen-1.json")
```

`read_annotation_manifest()` 仅返回已通过结构与跨记录语义检查的本地数组，不打开图片；需要核对图片时必须使用 `SampleStore.validate()`。调用方持有的 `records` 含私有标注，不应直接打印或上传。

CLI 成功返回 0，预期失败返回 2，失败只打印错误码和不含输入内容的说明。Python 接口对数据/工作流失败抛出 `WorkflowError`（含 `code`），磁盘等操作系统失败保留 `OSError` 供调用方处理，CLI 将其转为 `local_io_failed`。

| 错误码示例 | 含义及处理 |
| --- | --- |
| `duplicate_image` | 已有相同字节或相同解码像素，拒绝再次导入；近似帧仍需人工分组。 |
| `image_unreadable` / `unsupported_image` / `image_orientation` | 损坏、非单帧 PNG/JPEG、EXIF 方向未预处理。不会创建标注或资产。 |
| `source_output_forbidden` / `cloud_output_forbidden` / `unsafe_private_dir` | 目标位于源码、已知同步目录、非绝对目录或符号链接路径。 |
| `path_escape` / `output_exists` / `unsafe_permissions` | 路径越界、输出已存在或权限/硬链接不安全；拒绝覆盖。 |
| `invalid_annotations` / `invalid_json` | 标签契约不符、JSON 重复键、非有限数字或语法错误。 |
| `independent_review_required` / `acceptance_review_required` | 缺少独立复核；分集不能补做复核。 |
| `split_isolation` / `acceptance_duplicate` / `synthetic_split_forbidden` | 场次或组跨集合、验收重复代表、合成样本试图离开隔离区。 |
| `revision_conflict` / `workspace_busy` | 旧修订或并发占用；读取当前状态再执行。 |
| `asset_integrity` / `asset_geometry` / `asset_binding` | 文件、尺寸、旋转或引用与私有索引不一致，停止使用当前样本。 |

先验证，再以排他方式创建图片文件，最后原子替换 `workspace.json`。普通异常（包括模拟磁盘写入失败）时删除本次新建资产及临时文件，旧工作区不变；同名已有文件不会被当作本次临时文件删除。初始化失败也清理本次新建目录，不接管已有目录。此原子替换不等于跨文件、断电或 `SIGKILL` 的完整事务保证：进程在图像写入后、索引提交前被强制终止，可能留下未引用资产/临时文件，不能把它们当作已接纳样本；本版不自动清理未知恢复材料。

## 6. 尚待后续完成

本版提供可运行的数据入口、人工编辑文件和约束检查，未提供图形标注界面、近似去重算法/阈值、自动代表帧选择、原始录制时间线恢复或来源真实性认证。它能检查已声明组号的隔离，不能发现所有近似画面或证明人员独立。

真实图片导入、独立人工标注、至少 3 个真实场次/100 张代表画面/20 个不同局面的验收集、版本核实、逐字段准确率及真实时延均未完成。不得将合成工作流测试的通过率解释为识别准确率。
