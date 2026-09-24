# Third-party notices

知弦 PC 是独立开发的桌面应用，不代表 Jev / TypeSafe 或微信官方。

## Jev Windows

Source: https://github.com/jev-chat/jev-chat-windows

`src/app/capture.py`, `ocr.py`, `fill.py` and parts of the decision pipeline are adapted from this project. An unmodified source snapshot and its license are under `vendor/jev-chat-windows`.

MIT License

Copyright (c) 2026 rezoch340

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Jev Android

基于 Jev 聊天助手（https://github.com/jev-chat/jev-chat-jarvis）二次开发。
Android questions and knowledge-context behavior inform this application's analysis.
Source and upstream license/NOTICE snapshots are kept under `vendor/jev-chat-jarvis`.

## Runtime components

Python (PSF), PySide6 / Qt (LGPLv3 or commercial), RapidOCR (Apache-2.0), ONNX Runtime (MIT), NumPy (BSD), Windows Capture (MIT), Pillow (HPND), and their transitive dependencies retain their respective licenses. Qt libraries are dynamically linked and are distributed as replaceable files in this portable directory. Corresponding upstream source is available from https://code.qt.io/cgit/pyside/pyside-setup.git/ and https://code.qt.io/cgit/qt/ . Installed package metadata and license files are preserved where provided by packaging hooks.

WeFlow integration uses its local documented HTTP interface. No CipherTalk or WeFlow database-key extraction binaries are redistributed by this application.

The optional local WeChat database reader uses `sqlcipher3` (Python binding: zlib/libpng-style license in its distributed LICENSE; PyPI metadata: MIT) and SQLCipher (BSD-3-Clause). Source: https://github.com/coleifer/sqlcipher3 and https://github.com/sqlcipher/sqlcipher . The adapter studies CipherTalk's published database schema but does not copy its CC-BY-NC-SA source or bundle its proprietary DLLs. CipherTalk configuration is read only on the current PC. Its database key is kept in memory and is never written to Zhixian settings or a plaintext database mirror.
Some WeChat text payloads are Zstandard-compressed; `zstandard` (BSD-3-Clause, https://github.com/indygreg/python-zstandard) decodes those payloads locally with a bounded output size. Undecodable content is not treated as a replyable text message.

## Noto Sans SC

The UI bundles unmodified Noto Sans SC from Google Fonts under SIL Open Font License 1.1. The copyright and complete license are preserved in `ui/fonts/OFL.txt`. Source, pinned checksum, and build-time download details are in `ui/fonts/README.md`. No proprietary Windows font is redistributed.
