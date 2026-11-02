# 本局容量提示撤回回放 v1

这是一个离线、私有、只读的最小演示：消费调用方显式给出的三张已绑定 PNG 帧，在页面依次展示“容量候选有空位的条件提示 → 识别未知撤回 → 满额候选继续撤回”。实现位于 `src/jcc_hint_replay/`。

它不采集平板、不运行 OCR、不做主体或阶段识别，也不计算阵容、买卖或装备策略。页面从不输出上场对象、买卖动作或点击；`advice_eligible=false`、`automatic_action=null` 固定存在。

## 输入

私有清单在项目外，固定为 `hint-replay-v1`、`purpose=private_replay_lifecycle_only` 和 `source_kind=recovered_recording_development`。每帧必须显式提供绝对路径、SHA-256、请求时间、解码 PTS 与容量结果。读取时重新核对 PNG 字节摘要；未知帧不能携带前一帧数字。三帧状态必须正好是：

1. `development_candidate`，且显示差值大于零；
2. `unknown`，所有容量数字为 `null`；
3. `development_candidate`，且显示差值为零。

这样只能演示状态替换，没有把相邻时间点、候选值或人工目视变成真值。原帧、路径、摘要和生成页面均不能提交到公开仓库。

## 运行与输出

```sh
PYTHONPATH=src .venv/bin/python -B -m jcc_hint_replay \
  --manifest /ABSOLUTE/PRIVATE/manifest.json \
  --output /ABSOLUTE/PRIVATE/new-output-directory
```

输出目录必须尚不存在且位于项目外；工具创建权限为 0700 的目录和 0600 的 `report.html`、`review.private.json`。HTML 自包含、无脚本、无网络资源，适合直接在本机打开。

## 验证

```sh
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s src/jcc_hint_replay/tests -v
```

合成测试覆盖完整的提示／未知撤回／满额撤回、未知值不携带旧差值、摘要错配及错误的生命周期顺序。通过只证明该离线状态展示逻辑；不证明真实容量识别、用户看见提示的时延、操作合法性或上分表现。
