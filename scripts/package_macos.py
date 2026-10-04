"""Audit the app resources before preserving macOS bundle symlinks in a ZIP."""
from pathlib import Path
import hashlib
import json
import platform
import re
import subprocess
import sys

from package_portable import CHUNK_SIZE, SCAN_OVERLAP, _check_content, _safe_name

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from desk.version import VERSION

# Public GnuTLS cryptographic self-test vectors, compared byte-for-byte with
# lib/crypto-selftests-pk.c at 99189278b2938188607e7d0d20bc80cc29b16a9d.
# Only complete matching blocks in this exact native dependency are recognized.
GNUTLS_KAT_HASHES = frozenset({
    'd039c8119a029ab9f9c83c04d67002d887b6bc6026c4264402ab27cdf24cf138',
    '7c4c63ee462e0e700cd9e29c8e0f730b3f1b484c4abdd83f1e69fcd477c061fa',
    '91ea1699ff6b1a34b4a1d500a9c75a808441e47b9ea68da6fb0195e01ce1dc61',
    '293216817f0d585f0b54f225582f59e7b2e2a795b5f8002e2d71c5e3d49ce9b0',
    'f8766749b0c6f9b5661364568226af0ba7ae8d87c0073df24e440de4417521cd',
    '934b3e9bfd9936c5e30c7f0ed7d36a05e2044f755c0aa433e1d6bef6a68d1391',
    'a0b9679665e29ac220dfb0b366317e1b9dd5fe31e9def8b2e511669a041389ae',
    '5a09eb5df3674472eda0077d83cbccef72692c3d052ab393368ac57295439c58',
    'fed7079dd4491609e4c996e232f366ac434fa4cc9df19df363391d079d470964',
    'ef237ea8db4f2ae9ee100e8ced96d29b5dceb0e6a948443e6b8a00b1791f9ec9',
    'fa0b06a72461ec0a963dcfccb8d5b61bd88a6074fc7271573bff68ab86b8c1af',
    'a4d138d7ef9748464117b44fb9c0a4b5b85a1599a127d02690abaa96d03c16e6',
})
GNUTLS_PATHS = frozenset(f'Contents/{section}/cv2/{directory}/libgnutls.30.dylib'
                         for section in ('Resources', 'Frameworks') for directory in ('.dylibs', '__dot__dylibs'))
_BOUNDARY = b'-' * 5
_KAT_BLOCK = re.compile(_BOUNDARY + rb'BEGIN ([A-Z ]*PRIVATE KEY)' + _BOUNDARY
                        + rb'[^\x00]{1,16384}?' + _BOUNDARY + rb'END \1' + _BOUNDARY)
_MACHO = frozenset((b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xce',
                    b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'))


def audit_content(path, relative):
    with path.open('rb') as stream:
        native = stream.read(4) in _MACHO
        stream.seek(0)
        if native and relative.as_posix() in GNUTLS_PATHS:
            data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError('Cryptographic dependency exceeds audited size limit.')
            def reviewed(match):
                block = match.group()
                return b'\x00' * len(block) if hashlib.sha256(block).hexdigest() in GNUTLS_KAT_HASHES else block
            _check_content(_KAT_BLOCK.sub(reviewed, data), relative, True)
            return
        tail = b''
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b''):
            _check_content(tail + chunk, relative, native)
            tail = (tail + chunk)[-SCAN_OVERLAP:]


def audit_bundle(bundle):
    bundle = Path(bundle).resolve()
    if bundle.suffix != '.app' or not (bundle / 'Contents/MacOS/Zhixian').is_file():
        raise ValueError('Missing macOS application executable.')
    for path in bundle.rglob('*'):
        relative = path.relative_to(bundle)
        runtime_data = relative.as_posix() in {'Contents/Resources/cv2/data', 'Contents/Frameworks/cv2/data'}
        if (path.name.casefold() in {'data', 'work', 'credentials.json', 'history.json', 'knowledge.json', 'background.jpg', 'send-audit.jsonl', 'auto-reply.json', 'config.json', 'contacts.json', 'notes.json'} and not runtime_data) or path.suffix.casefold() in {'.db', '.db-wal', '.db-shm', '.sqlite', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm'}:
            raise ValueError('Application bundle contains private runtime data: ' + _safe_name(relative))
        if path.is_symlink():
            if not path.resolve().is_relative_to(bundle) or not path.exists():
                raise ValueError('Bundle link points outside the application.')
            continue
        if path.is_file():
            audit_content(path, relative)
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
    print(json.dumps({'archive': output.name, 'sha256': checksum, 'developer_id_signed': False, 'ad_hoc_signature': True}))


if __name__ == '__main__':
    main()
