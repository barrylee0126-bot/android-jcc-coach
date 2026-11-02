# 回放条件说明演示 v1

`jcc_replay_coach` 只把调用方已绑定的私有回放静帧做成可逐帧查看的本机页面。它不读取平板、不运行 OCR、不判断棋子或商店、不认证真实价格、主体、模式、版本或补丁，也不输出游戏建议、策略或动作。

输入是项目外、权限受限的 JSON 清单。每帧都有 SHA-256 绑定的 PNG、连续的请求时间；解码 PTS 可以明确保留为未知，不能用请求时间冒充实际 PTS。非空 PTS、候选源名称和候选源帧 SHA 都是调用方声明：模块只验证候选源帧 SHA 与展示图片字节一致，不会自行读取或认证 20C 的原 OCR 报告，也不会从视频复算 PTS。相邻帧请求时间差必须不超过五秒，并且仅拒绝相邻两张 PNG 的完整字节 SHA 相同。这一检查不能认证新画面、真实连续性或实际 PTS：同像素的重新编码、带不同元数据的 PNG、非相邻的 A→B→A 重用，仍可通过；若要防旧帧伪装，需要另行实现像素级或视频源连续性校验。页面仅允许既有 `video-economy-candidate-v1` 的同帧机器金币候选显示为：机器候选 `G`、显式假设支出 `P`、算术 `G - P`；它不把 `P` 称为商店实际价格，也不建议购买。

新帧候选为 `unknown` 时，旧帧的候选、假设和余额都会撤回。调用方还必须明确 `basis=no_fresh_candidate_supplied`，表示本帧没有交给模块新的可消费候选；它不能被写成 OCR 失败、真实数值或断流已被检测的证据。撤回原因只接受无可靠候选、候选过期、场景切换、来源断流、帧绑定缺失或候选冲突；输出不允许把数字带入未知帧；最后一帧必须为撤回状态，确保演示实际包含失效处理。输出目录只能新建在项目外的私有目录中，HTML 内嵌图片和少量本地交互脚本，CSP 禁止网络与外部资源；报告只留在本机。

报告的 `human_ground_truth_count=0`、`independent_review_count=0`、`accuracy=null`、`advice_eligible=false`、`strategy_recommendation=false`、`automatic_action=null` 是固定边界。页面逐帧由用户手动点击查看，不是自动连续视频播放，也不表示实时撤回。它用于检查“同帧机器候选 → 条件算术 → 新帧未知撤回”的行为，不是实时延迟、识别准确率、实战策略、自动操作或上分证据。

浏览器回归测试只使用临时目录里的合成 1×1 PNG 和合成清单，不读取私有录像或页面。运行 `node src/jcc_replay_coach/tests/test_web_visibility.mjs`；默认使用项目 `.venv/bin/python` 及常见 Chrome/Chromium 路径，也可用 `JCC_REPLAY_COACH_PYTHON`、`JCC_REPLAY_COACH_CHROME_BIN` 指定。测试由现有渲染器生成页面，在真实无头浏览器按计算样式和布局矩形判断可见性，不依赖 `hidden` 属性；它同时注入旧的 `article[hidden]{display:grid!important}` 作为负对照，确保该缺陷会被捕获。浏览器或项目 Python 不可用时，测试输出结构化 `skipped` 原因并以退出码 77 结束。
