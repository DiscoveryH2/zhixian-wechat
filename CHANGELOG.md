# Changelog

## 1.4.2 预览版

- 修复实时自动回复的语义门控：Jev 的 `should_reply_now` 判断的是「下一条是否要包含实质内容」，不能当作「现在是否应该发送」。现在单独做 typed 的回复时机判断，确定需要回复才起草和发送；即使合适的是简短过渡回复，也不会因缺少实质内容而误跳过。
- 知弦进程在真实发送键调用前后和气泡核验后写入脱敏审计事件，便于区分应用自动发送与人工点击；日志不含聊天正文、候选或 API Key。
- 发送期间收到的新来信会合并为最新一轮，当前一条确认完成后再重新判断，不再静默丢弃。
- 「当前会话单次回复」入口更清晰；显式选择群聊“评估所有新消息”时，单次补回也可考虑没有 @ 用户的群消息，但仍要求有可核对的先前己方消息作为未回复边界、通过更高的 Jev 置信度门槛，且只针对当前可见会话、最多一条。
- 群聊实时自动回复在“评估所有新消息”模式下增加较高置信度门槛；是否发送仍受风险、候选排序、会话核验、限额与固定署名约束。验证范围见[1.4.2 发行说明](docs/releases/1.4.2.md)。

## 1.4.1 预览版

- 修复历史补回复与实时自动回复的重复发送窗口：若入站消息 ID 已被实时自动回复记录为见过，历史补回复会跳过该消息，避免 UI 采集延迟期间再次发送。无新增真实发送验证；详情见[1.4.1 发行说明](docs/releases/1.4.1.md)。

## 1.4.0 预览版

- 增加一次性历史补回复流程：只处理用户明确选中的白名单会话和当前可见聊天；基于历史判断最近一个尚未回应的来信回合是否值得回复，最多发送一条。
- 对可确认时间戳超过 7 天的来信跳过。OCR 无法确认时间时，只有用户在当前会话明确确认后才继续，并要求 Jev 对目标和回复价值给出更强把握。
- 群聊仅在可靠确认消息定向发给当前用户，或明确提及当前用户群内显示名称时考虑回复；一般群聊讨论不触发。
- 在任何发送前再次进行 Jev 分析、风险与候选排序门控；保留固定披露后缀、输入框与当前会话核验、冷却/限额、一次性去重及失败后不自动重试。
- 不广播、不切换到屏幕外会话。验证结果与尚未验证的边界见[1.4.0 发行说明](docs/releases/1.4.0.md)。

## 1.3.0

- 增加可选的受限自动回复：每次启动默认关闭，需明确启用并设置会话 ID 与私聊/群聊类型白名单；提供冷却时间、每小时/每日上限、暂停和紧急停止。
- 仅处理白名单内的新收到纯文字消息。群聊默认要求可靠的“提及我”确认；窗口 OCR 无法可靠确认 @ 提及，因此 OCR 群聊需要用户明确选择“全部群消息”模式。
- 发送前重新验证当前可见微信会话、最新来信、空且聚焦的输入框和实际草稿内容；发现已有草稿、会话变化或状态不确定时拒绝发送。无法确认送达时暂停自动回复。
- 自动发送的回复恰好附加 `（以上内容为知弦生成）`。不支持主动发起消息或群发；不通过切换屏幕外会话或 WeFlow 发送 API 发消息。
- 自动回复配置写入本机明文 `data/auto-reply.json`，包含策略、白名单和去重状态，不保存聊天正文或回复草稿；联系人/会话 ID 仍属私有信息。发送只使用当前自动回复分析请求的云端模型上下文，匹配笔记不会进入自动草稿上下文。
- 测试状态与已验证环境以[1.3.0 发行说明](docs/releases/1.3.0.md)为准；合成测试不能证明真实微信环境的兼容性或消息送达。

## 1.2.0

- 增加会话目录、名称搜索、分来源浏览和历史分页；最近会话及当前页摘要在后台按需预热。
- 增加用户主动选择的 ChatLab JSON / JSONL 导入，使用本机 SQLite 索引管理归档；OCR、导入和 WeFlow 会话保留来源与独立 ID。
- 扩展 WeFlow 历史 API 适配：渐进加载会话索引、分页消息和按好友筛选朋友圈。服务不可用时明确退回已收集或导入的数据，不显示虚假的全量连接状态。
- 增加按需图片预览/理解与语音转文字入口；检查媒体原件、格式和模型能力。联网处理由显式操作触发，本地语音模型需自行准备，TTS 尚未实现。
- 增加手动动态与 WeFlow 朋友圈建议，区分点赞、公开评论、私聊或暂不回应；不自动执行互动，公开评论生成步骤不接收私聊全文。
- 增加主题、字号和自选背景，继续支持精简模式与置顶。
- 明确存储边界：API Key / Token 使用 DPAPI；导入归档为明文 SQLite，实时历史开关和“清空历史”不删除导入归档。

全量浏览仍依赖运行中的兼容 WeFlow 或用户提供的完整导出；窗口 OCR 只覆盖当前可见文字。图片/语音标签不代表实际媒体可读取，独立朋友圈媒体缓存也不能替代帖子索引。性能和功能验证应以当前测试及发行说明为准，不将规划中的验收目标写成实测结果。

## 1.1.0

- Refined desktop and compact layouts, clearer conversation/analysis/reply hierarchy, and a primary fill action.
- Bundled a verified OFL Chinese font for consistent offline typography.
- Added public documentation, privacy details, contribution guidance, and MIT licensing with upstream notices.
- Added source/index/history secret checks, reviewed screenshot hashes, and a stricter portable-package boundary.
- Added reproducible font retrieval and public release automation with synthetic diagnostics.

## 1.0.0

- Windows WeChat capture, local OCR, Jev judgments, reply drafting and ranking.
- Contact profiles, background notes, optional local history, and copy/fill without sending.
- Windows DPAPI credentials, stale-result protection, and cancellable model worker processes.
