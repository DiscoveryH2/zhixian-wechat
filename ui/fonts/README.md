# Bundled Chinese font

The interface uses **Noto Sans SC**, a variable sans-serif font covering Simplified Chinese, Latin and other supported scripts. Keeping the full font in the application makes rendering independent of Windows fallback font selection. No font request is made to an external server at runtime.

- Upstream: [Google Fonts / Noto Sans SC](https://github.com/google/fonts/tree/main/ofl/notosanssc)
- Original file: `NotoSansSC[wght].ttf` (renamed locally to `NotoSansSC-Variable.ttf`; font contents are unmodified)
- Direct download: <https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf>
- Downloaded: 2026-09-22
- Size: 17,772,300 bytes
- SHA-256: `a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da`
- License: [SIL Open Font License 1.1](OFL.txt)
- License source: <https://raw.githubusercontent.com/google/fonts/main/ofl/notosanssc/OFL.txt>
- License SHA-256: `1c05c68c34f9708415aada51f17e1b0092d2cea709bf4a94cd38114f9e73d7d9`

The copyright notice and complete license are preserved in `OFL.txt`. This font is not covered by the application's MIT license and must retain its OFL notice when redistributed. No proprietary Windows font is bundled.

The font binary is not stored in source Git. Run `python scripts/fetch_font.py` once to obtain it from the pinned upstream commit and verify the SHA-256 above. The build script does this automatically. Release packages include the verified file, so the application never needs to download fonts at runtime.
