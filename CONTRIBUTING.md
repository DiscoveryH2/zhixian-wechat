# 参与贡献

欢迎改进知弦 PC 的窗口兼容性、OCR、模型接口、界面体验和文档。较大的设计调整可以先开 Issue，说明想解决的问题、使用场景和预期行为。

## 开始开发

环境安装、目录结构、模型路由和打包流程见 [开发指南](docs/DEVELOPMENT.md)。推荐先运行 `--demo` 理解界面，再用合成对话和本机模拟服务进行开发。

## 提交 Pull Request

1. 围绕一个清晰问题修改，避免夹带无关重构。
2. 说明修改前后的行为，列出必要的复现步骤或截图。
3. 运行相关测试，并在涉及交互时检查正常工作台与精简模式。
4. 更新受影响的文档、配置说明或第三方许可。

常用验证命令：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src
.\.venv\Scripts\python.exe scripts/smoke_bridge.py
```

请准确区分单元测试、本机模拟服务、合成 OCR、真实窗口和外部 API 验证。不要把没有执行的检查写成通过。

## 实现约定

- 耗时工作不能阻塞 Qt 界面线程；后台任务需要可暂停和退出。
- 会话与消息使用明确的 ID 和方向；不靠相似名称自动合并不同联系人。
- 历史或不确定的新旧消息应保留标记，避免触发错误的自动分析。
- 回填必须核验当前目标；不添加默认自动发送，不通过按 Enter 或点击发送绕过用户确认。
- 模型返回缺失、失败或无法解析时，应如实显示，不生成看似真实的概率或伪装成功。
- 密钥保留在后端，远程 API 使用 HTTPS，跨服务来源不自动复用凭据。

## 数据与截图

测试夹具、PR 截图和公开演示必须使用合成数据。不要提交个人数据目录、API Key、Token、真实聊天、联系人、私人笔记、截图或环境变量转储。

运行正常模式时可用 `--data-dir work/dev-data` 隔离设置；公开截图使用 `--demo --screenshot`。即使 `credentials.json` 是加密文件，也不应提交。

## 许可与归属

贡献的新代码按本项目 [MIT License](LICENSE) 发布。请保留上游版权、LICENSE 和 NOTICE；使用第三方代码、字体、图标或其他资源时，注明来源及适用许可，并更新 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

知弦 PC 是独立项目，不应使用上游名称或标识暗示原作者或服务商背书。涉及安全和隐私的问题，请先阅读 [SECURITY.md](SECURITY.md)。
