# macOS 预览版

macOS 13 及以上可运行跨平台工作台：导入 ChatLab JSON/JSONL、浏览完整归档与群聊、检索正文、分析客户会话、扫描承诺、建立行动、图片理解、语音转写、本机朗读和朋友圈建议。全局会话弹窗覆盖当前已导入或已连接来源，按页加载。

macOS 暂不支持微信窗口 OCR、Windows CipherTalk 配置发现、原生微信加密数据库直读和自动填入/发送。这些能力在界面中按平台说明或隐藏。Mac 微信不同版本的数据库与媒体布局需要单独适配；没有已授权的数据来源，程序不能读取未导入的全账号聊天或朋友圈。兼容 WeFlow 本地服务可以使用，但本项目不附带该服务。

## 从源码运行

需要 Python 3.12、Homebrew 和本机 SQLCipher：

```bash
brew install python@3.12 sqlcipher
python3.12 -m venv .venv
SQLCIPHER_PREFIX="$(brew --prefix sqlcipher)"
LDFLAGS="-L$SQLCIPHER_PREFIX/lib" CPPFLAGS="-I$SQLCIPHER_PREFIX/include -I$SQLCIPHER_PREFIX/include/sqlcipher" .venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python scripts/fetch_font.py
.venv/bin/python src/main.py
```

先在「全局会话」中导入文件或目录，再选择需要分析的客户或群聊。`--demo` 展示合成数据，无需真实模型服务。API Key 存在 macOS 钥匙串中；设置、联系人和聊天归档保存在 `~/Library/Application Support/Zhixian`。钥匙串拒绝访问时配置保存会失败，不会改存明文密钥。背景图片与系统朗读不会上传。

## 构建与验证

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/smoke_workbench.py
bash scripts/build_macos.sh
```

产物为 `outputs/build/Zhixian.app` 和带架构名称的 macOS ZIP。构建必须在目标 Mac 架构上运行，不承诺一个包同时适配 Intel 与 Apple Silicon。CI 会运行 macOS 构建、合成测试与打包后自检；本机微信读取和送达不能由这些测试证明。

当前 macOS 包是开发者预览，未配置 Developer ID 签名与 Apple 公证。正式商用分发必须先完成签名、公证、升级与回滚验收；尚未完成时可使用源码运行。不要关闭系统安全机制来把预览包当成正式发行版。
