"""Build an allowlisted portable ZIP without including local user data.

Only standard-library modules are used. Suspicious names or secret-like bytes
abort the build; errors identify the relative filename, never matched content.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import zipfile


ROOT_FILES = frozenset({
    'zhixian.exe', 'readme.md', '使用说明.md', 'license', 'third_party_notices.md',
})
ROOT_DIRS = frozenset({'_internal', 'licenses', 'docs'})
EXCLUDED_ROOT_DIRS = frozenset({'data', 'work'})
RUNTIME_CONFIG_PACKAGES = frozenset({
    'cv2', 'numpy', 'onnxruntime', 'rapidocr_onnxruntime', 'pyside6', 'pil',
    'shiboken6', 'onnx', 'scipy', 'certifi', 'setuptools',
})
CHUNK_SIZE = 1024 * 1024
SCAN_OVERLAP = 8192
TOKEN_PATTERNS = (
    re.compile(rb'sk-or-v1-[A-Za-z0-9_-]{32,256}'),
    re.compile(rb'gh[pousr]_[A-Za-z0-9]{20,256}'),
    re.compile(rb'github_pat_[A-Za-z0-9_]{22,256}'),
)
PEM_BOUNDARY = b'-' * 5
PEM_PRIVATE_PATTERN = PEM_BOUNDARY + rb'BEGIN (?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY' + PEM_BOUNDARY
PGP_PRIVATE_PATTERN = PEM_BOUNDARY + b'BEGIN PGP PRIVATE KEY BLOCK' + PEM_BOUNDARY
PRIVATE_KEY_MARKER = re.compile(
    PEM_PRIVATE_PATTERN + b'|' + PGP_PRIVATE_PATTERN + rb'|PuTTY-User-Key-File-[23]:'
)
# Native TLS libraries legitimately embed NUL-terminated PEM parser constants.
# A PEM header followed by a line of encoded material is a private-key payload,
# including when it has been appended to an otherwise legitimate PE binary.
PRIVATE_KEY_PAYLOAD = re.compile(
    PEM_PRIVATE_PATTERN + rb'[ \t]*\r?\n(?:[A-Za-z-]+:[^\r\n]*\r?\n){0,4}'
    rb'(?:\r?\n)?[A-Za-z0-9+/=]{16,}'
    + b'|' + PGP_PRIVATE_PATTERN + rb'[ \t]*\r?\n|PuTTY-User-Key-File-[23]:[^\r\n]*\r?\n'
)
SENSITIVE_NAME = re.compile(
    r'(?:^|[._\- ])(?:credentials?|secrets?|tokens?|api[_-]?keys?|access[_-]?keys?'
    r'|private[_-]?keys?|id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:$|[._\- ])', re.I,
)
PRIVATE_RECORD_NAME = re.compile(
    r'(?:^|[._\- ])(?:history|knowledge|chat[_-]?history|chat[_-]?messages?'
    r'|conversations?|contacts|sessions)(?:$|[._\- ])', re.I,
)
REPORT_NAME = re.compile(
    r'(?:^|[._\- ])live(?:$|[._\- ])|(?:report|log)(?:[._\-].*)?\.(?:json|jsonl|txt|csv)$'
    r'|\.log(?:\.[0-9]+)?$', re.I,
)


class PackageError(RuntimeError):
    """A safe, content-free explanation of a rejected package input."""


@dataclass(frozen=True)
class PackageResult:
    archive: Path
    checksums: Path
    sha256: str
    file_count: int


def _safe_name(relative: Path | str) -> str:
    raw = str(relative).replace('\\', '/').encode('utf-8', errors='replace')
    for pattern in TOKEN_PATTERNS:
        raw = pattern.sub(b'[redacted]', raw)
    name = raw.decode('utf-8', errors='replace')
    return ''.join(c if c.isprintable() else '?' for c in name)[:240]


def _fail(reason: str, relative: Path | str):
    raise PackageError(f'{reason}: {_safe_name(relative)}')


def _runtime_config_allowed(relative: Path) -> bool:
    parts = [part.casefold() for part in relative.parts]
    return len(parts) >= 3 and parts[0] == '_internal' and parts[1] in RUNTIME_CONFIG_PACKAGES


def _check_name(relative: Path):
    name = relative.name.casefold()
    if name == '.env' or name.startswith('.env.') or name.startswith('.env-'):
        _fail('Environment file refused', relative)
    if name in {'.git', '.ssh', '.aws', '.azure', '.gnupg'}:
        _fail('Private metadata directory refused', relative)
    if SENSITIVE_NAME.search(name):
        _fail('Credential-like filename refused', relative)
    if PRIVATE_RECORD_NAME.search(name) and Path(name).suffix in {'.json', '.jsonl', '.csv', '.txt', '.db', '.sqlite', '.sqlite3'}:
        _fail('Private-record filename refused', relative)
    if REPORT_NAME.search(name):
        _fail('Log or live-report filename refused', relative)
    if Path(name).suffix in {'.key', '.pfx', '.p12', '.ppk', '.jks', '.keystore'}:
        _fail('Private-key container refused', relative)
    if name.startswith('config') and name.endswith('.json') and not _runtime_config_allowed(relative):
        _fail('Unapproved configuration file refused', relative)


def _check_regular_path(path: Path, relative: Path, directory=False):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        _fail('Link or junction refused', relative)
    try:
        stat = path.stat(follow_symlinks=False)
    except OSError:
        _fail('Cannot inspect input', relative)
    if getattr(stat, 'st_file_attributes', 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        _fail('Reparse point refused', relative)
    if not (path.is_dir() if directory else path.is_file()):
        _fail('Non-regular input refused', relative)


def collect_files(folder: Path) -> list[tuple[Path, Path]]:
    """Select roots before descent; excluded data/work are never read or scanned."""
    folder = Path(folder)
    if not folder.is_dir():
        raise PackageError('Input application directory does not exist')
    selected = []
    try:
        roots = sorted(folder.iterdir(), key=lambda p: p.name.casefold())
    except OSError:
        raise PackageError('Cannot list input application directory') from None
    for path in roots:
        relative = Path(path.name)
        lower = path.name.casefold()
        if lower in EXCLUDED_ROOT_DIRS:
            continue
        _check_name(relative)
        if lower in ROOT_FILES:
            _check_regular_path(path, relative)
            selected.append((path, relative))
        elif lower in ROOT_DIRS:
            _check_regular_path(path, relative, directory=True)
            def onerror(_):
                _fail('Cannot list package directory', relative)
            for current, directories, filenames in os.walk(path, followlinks=False, onerror=onerror):
                current = Path(current)
                directories.sort(key=str.casefold)
                filenames.sort(key=str.casefold)
                for name in directories:
                    child = current / name
                    child_relative = child.relative_to(folder)
                    _check_name(child_relative)
                    _check_regular_path(child, child_relative, directory=True)
                for name in filenames:
                    child = current / name
                    child_relative = child.relative_to(folder)
                    _check_name(child_relative)
                    _check_regular_path(child, child_relative)
                    selected.append((child, child_relative))
        # Unrecognized non-sensitive root files/directories are not distributable.
    if not any(relative.as_posix().casefold() == 'zhixian.exe' for _, relative in selected):
        raise PackageError('Required application file missing: Zhixian.exe')
    return selected


def _is_pe(stream) -> bool:
    """Recognize PE headers rather than trusting a .dll/.exe filename."""
    initial = stream.read(64)
    if len(initial) < 64 or initial[:2] != b'MZ':
        stream.seek(0)
        return False
    offset = struct.unpack_from('<I', initial, 0x3c)[0]
    if not 64 <= offset <= 1024 * 1024:
        stream.seek(0)
        return False
    stream.seek(offset)
    result = stream.read(4) == b'PE\0\0'
    stream.seek(0)
    return result


def _check_content(chunk: bytes, relative: Path, is_pe: bool):
    if any(pattern.search(chunk) for pattern in TOKEN_PATTERNS):
        _fail('Secret-like token content refused', relative)
    if (PRIVATE_KEY_PAYLOAD if is_pe else PRIVATE_KEY_MARKER).search(chunk):
        _fail('Private-key content refused', relative)


def _write_checked(archive: zipfile.ZipFile, source: Path, relative: Path):
    try:
        with source.open('rb') as stream:
            is_pe = _is_pe(stream)
            tail = b''
            with archive.open('Zhixian/' + relative.as_posix(), 'w', force_zip64=True) as dest:
                while True:
                    chunk = stream.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    _check_content(tail + chunk, relative, is_pe)
                    dest.write(chunk)
                    tail = (tail + chunk)[-SCAN_OVERLAP:]
    except PackageError:
        raise
    except OSError:
        _fail('Cannot read package input', relative)


def package_portable(input_dir: Path | str, output_zip: Path | str) -> PackageResult:
    folder = Path(input_dir).resolve()
    target = Path(output_zip).resolve()
    if target.is_relative_to(folder):
        raise PackageError('Output archive must be outside the input application directory')
    if target.suffix.casefold() != '.zip':
        raise PackageError('Output filename must end in .zip')
    files = collect_files(folder)
    target.parent.mkdir(parents=True, exist_ok=True)
    archive_temp = checksum_temp = None
    manifest = target.parent / 'SHA256SUMS'
    try:
        with tempfile.NamedTemporaryFile(prefix='.zhixian-package-', suffix='.zip.tmp', dir=target.parent, delete=False) as tmp:
            archive_temp = Path(tmp.name)
        with zipfile.ZipFile(archive_temp, 'w', zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for path, relative in files:
                _write_checked(archive, path, relative)
        with zipfile.ZipFile(archive_temp, 'r') as archive:
            bad = archive.testzip()
            if bad is not None:
                _fail('ZIP integrity check failed', bad)
        digest = hashlib.sha256()
        with archive_temp.open('rb') as stream:
            for chunk in iter(lambda: stream.read(CHUNK_SIZE), b''):
                digest.update(chunk)
        hexdigest = digest.hexdigest()
        with tempfile.NamedTemporaryFile(prefix='.zhixian-checksums-', suffix='.tmp', dir=target.parent, delete=False) as tmp:
            checksum_temp = Path(tmp.name)
            tmp.write(f'{hexdigest}  {target.name}\n'.encode('utf-8'))
        os.replace(archive_temp, target)
        archive_temp = None
        os.replace(checksum_temp, manifest)
        checksum_temp = None
        return PackageResult(target, manifest, hexdigest, len(files))
    except PackageError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError):
        raise PackageError('Cannot create or verify the portable archive') from None
    finally:
        for temporary in (archive_temp, checksum_temp):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('outputs/Zhixian'), help='Application directory (default: outputs/Zhixian)')
    parser.add_argument('--output', type=Path, default=Path('outputs/知弦-PC-1.1.0-Windows.zip'), help='Destination ZIP (default: outputs/知弦-PC-1.1.0-Windows.zip)')
    args = parser.parse_args(argv)
    try:
        result = package_portable(args.input, args.output)
    except PackageError as exc:
        print(f'Packaging refused: {exc}', file=sys.stderr)
        return 1
    print(f'Portable archive verified: {_safe_name(result.archive.name)} ({result.file_count} files)')
    print(f'SHA256SUMS written for {_safe_name(result.archive.name)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
