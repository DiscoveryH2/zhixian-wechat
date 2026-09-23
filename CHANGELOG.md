# Changelog

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
