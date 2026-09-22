"""Fetch the unmodified OFL font at a pinned commit; verify before use.

Only source developers/build runners run this. Portable users stay offline for
fonts. No credentials, user data, or executable code are involved.
"""
from pathlib import Path
import hashlib
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / 'ui/fonts/NotoSansSC-Variable.ttf'
SHA256 = 'a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da'
URL = ('https://raw.githubusercontent.com/google/fonts/'
       'a85815a42757630ce188fdad368c2dfc444d4773/ofl/notosanssc/NotoSansSC%5Bwght%5D.ttf')


def main():
    if TARGET.exists() and hashlib.sha256(TARGET.read_bytes()).hexdigest() == SHA256:
        print('Verified offline UI font.')
        return
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read(24 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise SystemExit('Font checksum mismatch; no unverified font was installed.')
    temporary = TARGET.with_suffix('.download')
    temporary.write_bytes(data)
    temporary.replace(TARGET)
    print('Downloaded and verified the OFL UI font.')


if __name__ == '__main__':
    main()
