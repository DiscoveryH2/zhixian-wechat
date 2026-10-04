"""Desktop capabilities and writable application locations."""
from pathlib import Path
import sys


def capabilities(platform=None):
    platform = platform or sys.platform
    windows = platform == 'win32'
    return {'platform': platform, 'name': 'macOS' if platform == 'darwin' else 'Windows' if windows else 'Linux',
            'native_capture': windows, 'native_database': windows, 'send': windows,
            'imports': True, 'media': True, 'moments': True,
            'credentials': 'Keychain' if platform == 'darwin' else 'DPAPI' if windows else 'session'}


def data_directory(root, demo=False):
    if demo:
        if sys.platform == 'darwin':
            return Path.home() / 'Library/Caches/Zhixian/demo'
        return Path(root) / 'work/demo-data'
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/Zhixian'
    return Path(root) / 'data'


def keychain_service(directory):
    import hashlib
    identity = str(Path(directory).resolve()).encode('utf-8')
    return 'ai.zhixian.desktop.' + hashlib.sha256(identity).hexdigest()[:32]


def keychain_get(directory):
    from keyring.backends.macOS import Keyring
    return Keyring().get_password(keychain_service(directory), 'credentials') or '{}'


def keychain_set(directory, value):
    from keyring.backends.macOS import Keyring
    Keyring().set_password(keychain_service(directory), 'credentials', value)
