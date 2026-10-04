# 1.8 回归与性能记录

## 本地验证

Linux / Python 3.12 / Qt offscreen，2026-10-04：332 项单元与契约测试，327 通过、5 平台相关跳过。新增 22 项覆盖跨账号/同名隔离、私聊群聊关联、全统计/跨时间抽样、朋友圈幂等/事务、旧库迁移、媒体解释失效、模拟记忆与真实原文隔离、引用伪造拒绝、跨服务密钥隔离、晚到/并发结果拒绝、删除竞态、全历史未抽样旧记忆检索、停用后检索排除、XML 实体拒绝与只读分页、同序号/同时间/同本地 ID 分片翻页防遗漏、过长/结构化 ID 拒绝而不截断合并。

合成桌面 UI 通过 23 阶段真实 QtWebChannel 流程：行动创建/完成、旧消息检索/定位、外观、承诺提案确认、完整历史统计、洞察模型进程、分身创建/模型对话/标签/来源记忆、停用记忆后清理对话。模型为本机 HTTP stub，实际使用隔离 spawn 模型进程；无真实微信或外部模型调用。Windows/macOS 原生 CI 均重复并通过此流程；原生 offscreen 未观察到开场绘制，不能据此证明原生开场动画实机效果。

运行时自检通过：SQLCipher、Zstandard、打包中文 OCR 资源、本机类型化接口、Qt WebEngine、关系库、独立分身模型进程、引用与持久对话。源码与历史发布扫描无发现；Python/JavaScript 语法与 diff 检查通过。原生 CI 也完成打包后的分身模型进程自检、资产与签名审计，均通过；Mac 有 ad-hoc 签名，无 Developer ID 公证。

## 百万条档案基准

同一 Linux 工作区，100 万条合成短文字、1000 会话：导入 15.436 秒、FTS 预热 1.977 秒、第一页目录 0.41ms；中文稀有词检索 P95 2.72ms，全匹配常见词 P95 380.67ms。每页最多 20 条，旧消息定位验证通过。

这些是一次合成实验，无外部模型和真实媒体；不能作多设备 SLA、模型质量、语义理解或完整微信兼容证明。新身份/媒体索引的导入开销已计入。单会话 100 万条基准：导入 25.605 秒；完整统计 2533.29ms，180 条时间分层抽样 1945.39ms，分身建立 5330.31ms，按问题全索引检索 10.85ms；最终只返回 24 条模型来源。常见词全文检索 P95 637.41ms。不同会话分布影响性能，不能套用单一耗时。

## 发布边界

Windows/macOS 原生构建和打包验证见 [Desktop checks and release](https://github.com/DiscoveryH2/zhixian-wechat/actions/workflows/windows.yml)。[本轮成功 CI](https://github.com/DiscoveryH2/zhixian-wechat/actions/runs/37199339303) 对发行提交 `f926a16bda78f283adb0c08c95ef2c894ba17052` 验证：Windows 332 项中 330 通过/2 跳过；macOS 326/6。首轮曾发现两个测试数据库连接未关闭的 Windows 夹具问题，已修复，不跳过失败测试。

[v1.8.0 release](https://github.com/DiscoveryH2/zhixian-wechat/releases/tag/v1.8.0) 已发布为 prerelease；标签指向上述提交。公开 SHA256 清单与 GitHub 资产 digest 核对一致：

| 资产 | SHA256 |
| --- | --- |
| Zhixian-1.8.0-Windows.zip | `7162d3c32904863c5a4591cf9774577e7a5fae9a8d8fa8e1c93559c3d6dd3996` |
| Zhixian-1.8.0-macOS-arm64.zip | `8cb62cf255f1c3ac5383c090fb0ea89eaaf3d7f9c0678ea1c768aa69057defba` |

本轮未用真实个人数据库做内容/媒体兼容验收，未验证真实微信正向发送，未使用外部模型评价分身相似度。Mac 原生读取/发送、签名公证、加密数据、语音复刻与语义向量检索仍待实施；不得写为已完成。
