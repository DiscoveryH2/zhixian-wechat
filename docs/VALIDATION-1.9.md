# 1.9.0 回归记录

更新时间：2026-10-25。环境：Windows 11、微信 4.1.15.56、Python 3.12.14、PySide6 6.11.2。

| 项目 | 结果 |
|---|---|
| 全量单测 `unittest discover -s tests` | 371 通过，3 跳过（字体缺失 1、Windows 无权创建 symlink 2） |
| `scripts/smoke_bridge.py`（真实 QWebChannel + DPAPI + 子进程模型 + 本地 HTTP） | 17/17 阶段通过，含工作 Agent 附件、产物预览、停止与重试、会话巡检 |
| `scripts/smoke_workbench.py` | 23/23 阶段通过 |
| `scripts/check_secrets.py` | 通过 |
| 真机只读监听 90 秒（同机同库对照） | 1.8.0：实时入站 0 条；1.9.0：实时入站文字 4 条 |
| 真机窗口枚举 | 微信主窗口 1 个 + 隐藏 `Weixin` 工具窗 2 个（176×199）；1.9 仅选中主窗口 |
| 真机群消息 | 108 条群文字中 97 条带 `wxid:` 前缀，已剥离；`source` 列 `<atuserlist>` 可识别 @我 |

未验证：真机自动发送（搜索切换会话 + OCR 核验 + 回车）、macOS 真机数据库读取。前者需要向真实联系人发消息，须经用户确认后进行。
