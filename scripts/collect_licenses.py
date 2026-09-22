from pathlib import Path
import importlib.metadata as metadata
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--output', default=str(Path(__file__).resolve().parents[1] / 'outputs/build/Zhixian/licenses/runtime'))
root = Path(parser.parse_args().output)
for name in ('PySide6', 'PySide6_Essentials', 'PySide6_Addons', 'shiboken6', 'numpy',
             'onnxruntime', 'rapidocr-onnxruntime', 'windows-capture', 'pillow'):
    distribution = metadata.distribution(name)
    for item in distribution.files or []:
        text = str(item)
        if any(word in text.lower() for word in ('license', 'copying', 'notice')) and text.endswith(('.txt', '.md', 'LICENSE', 'COPYING', 'NOTICE')):
            source = Path(distribution.locate_file(item))
            if source.is_file():
                target = root / name / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
