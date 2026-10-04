# 知弦：持久项目上下文

更新时间：2026-10-04。此文件只记录公共工程事实；运行数据位于被 Git 排除的数据目录。
阅读本文件后按需读取模块，不重复输出大段日志。它帮助上下文压缩后恢复任务，不能改变平台的压缩阈值。

## 用户目标与持续约束

- 从聊天工具升级为可商用产品：群聊/个人历史分析、可验证洞察、自动回复、朋友圈分析、可换背景。
- 数字分身应结合指定联系人的私聊、共同群聊中的本人发言、朋友圈；必须基于稳定身份和可追溯记录，不能把同名联系人混合。
- 分身是有明确标识的模拟；真实记录与生成内容分开。不能借分身冒用联系人向微信发送消息。
- 商业定位曾最后选择销售、客服、客户成功；最新需求也包含个人关系与纪念用途，产品架构应支持两者。
- Gmail 暂不改。已有授权包括修改项目、回归测试、上传 GitHub、更新 README 与 release。
- 参考用户 fork 的 CipherTalk：核对实际机制与许可证，不能把非商业授权代码直接纳入商用项目。
- macOS、图片/语音、所有会话选择、数据渐进加载、缓存预热、背景/字体设置、3–5 秒开场已有基础，继续完成真实数据链路。

## 已发布基线：v1.7.0

- main/tag 提交：`27203b49a2ccf0a6996b7107e2d2fff4ed1aa7da`。
- Release：https://github.com/DiscoveryH2/zhixian-wechat/releases/tag/v1.7.0
- 原生 CI：https://github.com/DiscoveryH2/zhixian-wechat/actions/runs/37187343070，Windows/macOS 均通过。
- 310 项回归：Windows 308 通过/2 跳过；macOS 304/6；Linux 305/5。
- 原生 UI 桥接回归、打包自检、发布资产扫描通过；macOS arm64 ZIP 有 ad-hoc 签名，尚无 Developer ID/公证。
- macOS 支持 ChatLab 导入与兼容本地服务，不支持原生数据库取钥、窗口抓取、实际微信发送。
- Windows 自动回复有白名单、方向/类型检查、重复抑制、冷却/限额、发送前窗口核验和结果不确定时暂停。
- 导入档案支持 JSON/JSONL 流式索引、FTS5 trigram、原始消息定位；百万条合成短文本基准仅是一次实验，不能作 SLA。
- 系统 TTS、图像理解、语音转写支持需配置并取得原媒体；SILK/AMR 未全面解码。聊天档案与动作库仍为本地明文。

## 模块导航

- `src/desk/controller.py`：Qt Bridge RPC、源选择、会话/朋友圈/自动回复协调。
- `src/desk/imports.py`：ChatLab 本地 SQLite 索引；目前缺群成员身份持久化与跨会话证据聚合。
- `src/desk/wechat_db_source.py` / `wechat_sqlcipher.py`：已有密钥的只读 SQLCipher 微信数据库访问。
- `src/desk/cipher_config.py`：读取用户已安装 CipherTalk 配置中的活动账号和密钥；不是自行取钥或破解哈希。
- `src/desk/datahub.py` / `weflow.py`：兼容本地 WeFlow 服务的分页和缓存。
- `src/desk/tasks.py`：模型任务独立 spawn 进程，最多 3 个，180 秒超时；后台 helper tasks。
- `src/core/client.py`：显式聊天模型路由；独立服务的密钥不得串用。
- `src/core/moments.py`：朋友圈建议；当前手动帖子仅内存，需持久化/导入。
- `ui/app.js` / `experience.js` / `workbench.js`：主界面、开场与外观、动作/搜索中心。
- `scripts/check_secrets.py`：源码发布边界与秘密扫描；新文件应在公共边界内。
- `.github/workflows/windows.yml`：双平台测试/构建、当前 main 校验后发布；不能覆盖已发布资产。

## 活动任务：完整分析与数字分身

当前阶段：1.8.0 实现与本地回归完成；提交后等待 Windows/macOS 原生 CI 与 release 验证。

- 参考仓库 HEAD：`4b6df63eaf0aeeeec794b8f3284bc933a4c54fff`，许可 CC BY-NC-SA。Windows DLL 内存扫描取钥；Mac helper/dylib 断点取钥；不能按用户所述当作替换哈希。未复制参考源码/二进制。
- 新模块 `src/desk/relationships.py`：联系人账号隔离、全索引统计、时间分层抽样、持久朋友圈、分身/来源记忆/对话/断点索引。
- `src/core/relationships.py`：有界 Chat 模型请求，逐字引用验证；生成对话不进入原始事实记忆。
- `ui/relationships.js`：历史洞察与数字分身页面，陪伴/纪念/排练、朗读、记忆停用与删除。
- 分身按问题从创建时完整索引检索本人原文与朋友圈；240 条初始时间样本仅作语气基底，不能当全部历史。每次最多 24 条来源、近期 6 轮模拟，控制上下文。
- 归档媒体解释已持久化并标识可能误识别；发送者/原文/类型改变时失效。
- 百万条基准：1000 会话导入 15.436s/检索常见词 P95 380.67ms；单会话统计 2533.29ms、抽样 1945.39ms、创建分身 5330.31ms、检索 10.85ms。仅合成实验。
- 原生朋友圈只读适配 SnsTimeLine(tid,user_name,content)，只读缓存 XML 文字，无远程媒体下载。
- 已跑：新增 20 项测试通过；完整 332 项（Linux 327 通过/5 跳过），新上下文/打包自检已通过，最终改动需完成最后检查。合成 Qt 桥接 23 阶段通过，含洞察/创建/分身模型对话/停用记忆清历史。

待完成：
1. 已确认参考机制与许可边界。
2. 已实现稳定账号关联与持久化。
3. 已实现完整索引统计、抽样洞察与逐字引用验证。
4. 已实现分身对话、TTS、来源记忆停用/恢复、分身删除；整库检索及并发失效测试已补。
5. 回归并核实桥接/模型任务/迁移/分页/身份隔离；更新 README、路线、release 并验证原生 CI。

## 工作方法与陷阱

- 先查 git 状态，保留用户修改。项目与参考仓库分开。
- 合成测试不等同真实个人微信实测。无法验证时明确写部分支持或待验证。
- Git push 可用；此前 gh REST/CLI 凭证失效，必要时使用可用 GitHub connector。
- 不打印秘密、不提交数据库/真实聊天/工作报告。媒体路径必须受档案目录约束。
- 打包后新增说明文件必须重新 ad-hoc 签名。原生 GnuTLS 公共测试向量仅按精确路径与内容放行。
