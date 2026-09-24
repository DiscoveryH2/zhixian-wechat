# 开发与构建

知弦 PC 使用 Python 3.12、PySide6 / QtWebEngine 和本地网页界面。Windows 采集功能需要 Windows；部分纯 Python 单元测试可以独立运行，但正式客户端和发行包以 Windows 为目标。

## 准备环境

在仓库根目录执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe scripts/fetch_font.py
.\.venv\Scripts\python.exe src/main.py
```

`requirements.txt` 声明直接依赖范围，`requirements-lock.txt` 保存用于复现的依赖版本。更新运行依赖时，请同步检查锁定文件、打包规则和第三方许可。

## 项目结构

```text
src/
  main.py                 Windows 桌面壳、托盘与启动参数
  desk/                   控制器、数据存储、界面桥接、消息源
  core/                   Jev 判断、回复生成、协议与模型路由
  app/                    Windows 捕获、OCR、安全粘贴与已核验发送
ui/                       本地 HTML、CSS、JavaScript 和界面资源
tests/                    单元测试、合成 OCR 和模拟服务测试
scripts/                  构建、许可收集和验证脚本
vendor/                   上游源码与原始版权、LICENSE、NOTICE
docs/                     公开文档与合成演示截图
```

`data/` 是个人运行数据；`work/`、`outputs/` 和 `.venv/` 是本机构建或开发产物。它们不应随源码发布。

## 不接触真实聊天的开发方式

```powershell
.\.venv\Scripts\python.exe src/main.py --demo
.\.venv\Scripts\python.exe src/main.py --demo --compact
```

演示模式使用明确标记的合成数据，不读取微信、不调用外部模型。测试正常模式界面时，可通过 `--data-dir work/dev-data` 隔离个人设置。

截图功能必须与演示模式组合使用，例如：

```powershell
.\.venv\Scripts\python.exe src/main.py --demo --screenshot work/workspace-preview.png
```

不要把真实截图、标题、联系人、聊天正文或凭据放进测试夹具、提交、CI 日志和 Issue。

## 界面与后端

界面通过 QtWebChannel 调用后端，后端返回状态快照并推送事件。后端持有密钥；公开配置只包含对应的“已配置”标志。耗时采集、OCR 和模型请求不应占用 Qt 界面线程。

事件与状态的实际定义见 [controller.py](../src/desk/controller.py) 和 [bridge.py](../src/desk/bridge.py)；修改接口时，需要同时检查控制器、桥接、前端和对应测试。消息方向使用 `me` / `other`，历史或无法确认是新消息的帧应保留 `historical` 标记，避免滚动历史触发新消息自动分析。

## 模型路由

| 判断服务配置 | 实际判断端点 |
| --- | --- |
| OpenRouter 的 `/api` 或 `/api/v1` 基础地址 | `/api/alpha/decisions` |
| TypeSafe `/v1` 基础地址 | `/v1/systemone` |
| 自定义 `.../systemone` 或 `.../decisions` | 对应完整端点 |
| 自定义 `/v1` 或 `/api` 基础地址 | 按当前路由规则补全 Jev 端点 |

Jev 请求使用 `state` 与 `questions`，响应需包含有效的 typed `answers`。普通 Chat Completions 文本不能冒充结构化判断结果；缺失判断或概率应保持缺失状态。

回复生成使用 Chat Completions。OpenRouter 默认配置可以复用同一来源的 Key；跨来源的生成服务必须使用独立 Key。默认回复模型在 `src/core/client.py` 中定义，修改时应同步检查 README 的配置说明。

一次分析通常包含判断与候选起草，取得多个候选后再进行 Jev 排序。生成或排序失败不应丢弃已经成功的判断。测试连接使用内置合成文本，不应读取当前聊天。

## 测试

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe scripts/smoke_bridge.py
```

单元测试覆盖模型协议、路由和错误处理、消息方向和去重、安全回填拒绝、存储及会话状态；合成 OCR 测试使用本机离线模型识别生成的中文图像。`smoke_bridge.py` 通过本机模拟接口检查 QtWebChannel 到后端的完整链路。

自动回复测试应优先使用合成会话、虚拟时钟和模拟输入框。发送路径必须在按键之前重新确认当前可见目标、最新来信、输入框为空且聚焦、以及填入文本与带披露后缀的预期草稿一致；不能通过屏幕外切换或 WeFlow 发送接口发送。真实微信验证需单独记录为真实窗口测试，合成测试不代表真实送达。

1.4.0 的一次性历史补回复需额外覆盖明确选择和白名单限制、最近未回应回合判定、最多一次发送、7 天时间边界、未知时间的当前会话确认与较强 Jev 门槛，以及群聊定向/明确提及判定。一般群聊讨论、已回应回合、屏幕外会话和超过时限的消息都应验证为跳过。发送前的第二次 Jev 分析、风险和排序门控、披露后缀、既有输入框核验、冷却/限额、去重与不重试行为均应覆盖。状态不明时不得重试发送。真实使用测试需要区分合成会话与真实服务调用，并在发行说明标记为待验证，直至实际完成。

这些测试不能证明所有微信版本、DPI、布局或外部模型服务均兼容。报告验证结果时，区分模拟服务、合成 OCR、真实窗口采集和外部服务调用，不把其中一种验证写成另一种。

## 构建便携版

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
.\.venv\Scripts\python.exe scripts/package_portable.py --input outputs/build/Zhixian --output outputs/Zhixian-1.3.0-Windows.zip
```

构建脚本先运行测试，再使用 PyInstaller 生成 `outputs/build/Zhixian/`，收集上游和运行组件的许可文件。打包脚本从该目录生成便携 ZIP，排除应用个人数据文件。构建目标如果已有用户数据，脚本会拒绝覆盖；可通过 `-OutputDirectory` 选择新的构建目录。

构建后应在完整目录中启动 `Zhixian.exe`；不要只复制一个 EXE。离线 OCR 模型、Qt 运行组件和界面资源都需要保留。

## 发布前

1. 运行测试、合成演示界面和打包启动验证。
2. 检查应用版本、发行文件名与[发行说明](releases/1.4.0.md)一致。
3. 检查 ZIP 清单，确认不含 `data/`、真实聊天、凭据、私人笔记或开发环境。
4. 保留本项目 LICENSE、上游 LICENSE / NOTICE 和第三方运行组件许可；新增字体等资源也要包含对应许可。
5. 使用合成数据制作公开截图，说明已验证的环境和仍存在的兼容性限制。

发布和贡献约定见 [CONTRIBUTING.md](../CONTRIBUTING.md)；敏感问题见 [SECURITY.md](../SECURITY.md)。
