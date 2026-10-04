"""Audit the app resources before preserving macOS bundle symlinks in a ZIP."""
from pathlib import Path
import hashlib
import json
import platform
import subprocess
import sys

from package_portable import CHUNK_SIZE, SCAN_OVERLAP, _check_content

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.version import VERSION


def audit_bundle(bundle):
    bundle = Path(bundle).resolve()
    if bundle.suffix != '.app' or not (bundle / 'Contents/MacOS/Zhixian').is_file():
        raise ValueError('Missing macOS application executable.')
    for path in bundle.rglob('*'):
        relative = path.relative_to(bundle)
        if path.name.casefold() in {'data', 'work', 'credentials.json', 'history.json', 'knowledge.json', 'background.jpg', 'send-audit.jsonl', 'auto-reply.json', 'config.json', 'contacts.json', 'notes.json'} or path.suffix.casefold() in {'.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm'}:
            raise ValueError('Application bundle contains private runtime data.')
        if path.is_symlink():
            if not path.resolve().is_relative_to(bundle) or not path.exists():
                raise ValueError('Bundle link points outside the application.')
            continue
        if path.is_file():
            with path.open('rb') as stream:
                magic = stream.read(4)
                native = magic in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf')
                stream.seek(0)
                tail = b''
                for chunk in iter(lambda: stream.read(CHUNK_SIZE), b''):
                    _check_content(tail + chunk, relative, native)
                    tail = (tail + chunk)[-SCAN_OVERLAP:]
    required = ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'licenses', 'docs')
    if any(not (bundle / 'Contents/Resources' / name).exists() for name in required):
        raise ValueError('Application bundle is missing documentation or licenses.')


def main():
    if sys.platform != 'darwin':
        raise SystemExit('macOS packaging requires macOS.')
    root = Path(__file__).resolve().parents[1]
    bundle = root / 'outputs/build/Zhixian.app'
    audit_bundle(bundle)
    output = root / f'outputs/Zhixian-{VERSION}-macOS-{platform.machine()}.zip'
    if output.exists():
        raise SystemExit('Release archive already exists; use a clean output directory.')
    subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(bundle), str(output)], check=True)
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    (output.parent / (output.name + '.sha256')).write_text(f'{checksum}  {output.name}\n')
    print(json.dumps({'archive': output.name, 'sha256': checksum, 'signed': False}))


if __name__ == '__main__':
    main()
