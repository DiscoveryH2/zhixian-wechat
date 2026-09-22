# PyInstaller onedir: offline OCR models, QtWebEngine, and local UI travel together.
from PyInstaller.utils.hooks import collect_all
from pathlib import Path

root = Path(SPECPATH)
datas = [(str(root / 'ui'), 'ui'), (str(root / 'THIRD_PARTY_NOTICES.md'), '.')]
binaries, hiddenimports = [], []
for module in ('rapidocr_onnxruntime', 'windows_capture'):
    d, b, h = collect_all(module)
    datas += d
    binaries += b
    hiddenimports += h
a = Analysis([str(root / 'src/main.py')], pathex=[str(root / 'src')],
             binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], runtime_hooks=[], excludes=['tkinter', 'matplotlib', 'scipy', 'IPython', 'pandas'],
             noarchive=False)
# Qt's Windows build imports the OS ICU ABI (un-suffixed functions). An ICU
# from tools such as Poppler on PATH exports suffixed symbols and breaks Qt.
# Supported Windows versions provide the required system ICU themselves.
a.binaries = [entry for entry in a.binaries if Path(entry[0]).name.lower() not in {'icuuc.dll', 'icudt78.dll'}]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Zhixian', debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / 'ui/icon.ico'))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='Zhixian')
