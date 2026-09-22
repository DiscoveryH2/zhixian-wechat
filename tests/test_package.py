"""Portable packaging tests use only tiny synthetic files and fake credentials."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'package_portable.py'
SPEC = importlib.util.spec_from_file_location('zhixian_package_under_test', SCRIPT)
packager = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = packager
SPEC.loader.exec_module(packager)


def fake_pe(payload=b''):
    header = bytearray(128)
    header[:2] = b'MZ'
    struct.pack_into('<I', header, 0x3c, 64)
    header[64:68] = b'PE\0\0'
    return bytes(header) + payload


def fake_pem_header():
    return b'-' * 5 + b'BEGIN PRIVATE KEY' + b'-' * 5


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'application'
        self.output = self.root / 'release' / '知弦-PC-1.1.0-Windows.zip'
        self.write('Zhixian.exe', b'tinyfake.exe')
        self.write('README.md', b'# Synthetic application')
        self.write('LICENSE', b'MIT License - synthetic fixture')

    def write(self, name, content):
        path = self.app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def package(self):
        return packager.package_portable(self.app, self.output)

    def assert_rejected(self, expected_filename, secret=None):
        with self.assertRaises(packager.PackageError) as ctx:
            self.package()
        message = str(ctx.exception)
        self.assertIn(expected_filename, message)
        if secret:
            self.assertNotIn(secret, message)
        self.assertFalse(self.output.exists())
        if self.output.parent.exists():
            self.assertEqual(list(self.output.parent.glob('.zhixian-*')), [])

    def test_allowlist_integrity_and_sha256(self):
        self.write('使用说明.md', '合成说明'.encode())
        self.write('THIRD_PARTY_NOTICES.md', b'Notices')
        self.write('docs/help.md', b'Help')
        self.write('licenses/example-license.txt', b'MIT')
        self.write('_internal/runtime.bin', b'public runtime bytes')
        self.write('personal-plan.md', b'This root file must not ship')
        self.write('extra/nonprivate.txt', b'Unapproved directory must not ship')
        result = self.package()
        with zipfile.ZipFile(result.archive) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            self.assertIn('Zhixian/Zhixian.exe', names)
            self.assertIn('Zhixian/docs/help.md', names)
            self.assertIn('Zhixian/使用说明.md', names)
            self.assertNotIn('Zhixian/personal-plan.md', names)
            self.assertNotIn('Zhixian/extra/nonprivate.txt', names)
        expected = hashlib.sha256(result.archive.read_bytes()).hexdigest()
        self.assertEqual(result.sha256, expected)
        self.assertEqual(result.checksums.read_text(encoding='utf-8'), f'{expected}  {result.archive.name}\n')

    def test_private_root_data_and_work_never_scanned(self):
        token = b'sk-or-v1-' + b'A' * 64
        self.write('data/credentials.json', token)
        self.write('data/history.json', b'private')
        self.write('work/.env', token)
        original = Path.open
        app = self.app
        def checked_open(path, *args, **kwargs):
            if path.is_relative_to(app):
                self.assertNotIn(path.relative_to(app).parts[0].lower(), ('data', 'work'))
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', checked_open):
            result = self.package()
        with zipfile.ZipFile(result.archive) as archive:
            self.assertFalse(any(name.startswith(('Zhixian/data/', 'Zhixian/work/')) for name in archive.namelist()))

    def test_legitimate_internal_data_and_runtime_config_remain(self):
        self.write('_internal/cv2/data/haarcascade.xml', b'<opencv_storage/>')
        self.write('_internal/cv2/data/config.json', b'{"resource":"synthetic"}')
        self.write('_internal/certifi/cacert.pem', b'-----BEGIN CERTIFICATE-----\nPUBLIC\n-----END CERTIFICATE-----')
        result = self.package()
        with zipfile.ZipFile(result.archive) as archive:
            self.assertIn('Zhixian/_internal/cv2/data/haarcascade.xml', archive.namelist())
            self.assertIn('Zhixian/_internal/cv2/data/config.json', archive.namelist())
            self.assertIn('Zhixian/_internal/certifi/cacert.pem', archive.namelist())

    def test_sensitive_names_refused_without_reading_content(self):
        cases = ('.env', '.env.local', 'credentials.json', 'config.json',
                 'docs/history.json', 'docs/knowledge.json', '_internal/api-token.txt',
                 'docs/server.key', 'docs/debug.log', 'docs/live-capture-status.json',
                 'docs/self-test-report.json', '_internal/config.json')
        for name in cases:
            with self.subTest(name=name):
                path = self.write(name, b'CONTENT MUST NOT BE READ')
                with patch.object(Path, 'open', side_effect=AssertionError('Suspicious filename was read')):
                    self.assert_rejected(name)
                path.unlink()

    def test_openrouter_and_github_tokens_fail_without_disclosing_value(self):
        tokens = (b'sk-or-v1-' + b'A' * 64, b'ghp_' + b'B' * 36,
                  b'gho_' + b'C' * 36, b'github_pat_' + b'D' * 70)
        for token in tokens:
            with self.subTest(prefix=token[:4]):
                path = self.write('docs/example.txt', b'prefix ' + token + b' suffix')
                self.assert_rejected('docs/example.txt', token.decode())
                path.unlink()

    def test_token_split_across_chunks_is_detected(self):
        token = b'sk-or-v1-' + b'A' * 64
        self.write('_internal/resource.bin', b'x' * 60 + token + b'end')
        with patch.object(packager, 'CHUNK_SIZE', 64):
            self.assert_rejected('_internal/resource.bin', token.decode())

    def test_private_key_header_in_text_is_refused(self):
        self.write('docs/example.pem', fake_pem_header() + b'\n')
        self.assert_rejected('docs/example.pem')

    def test_tls_pe_parser_constant_is_not_a_private_key(self):
        self.write('_internal/native.dll', fake_pe(b'PEM parser\0' + fake_pem_header() + b'\0'))
        self.package()

    def test_private_key_payload_appended_to_pe_is_refused(self):
        self.write('_internal/native.dll', fake_pe(fake_pem_header() + b'\n' + b'QUJD' * 32 + b'\n'))
        self.assert_rejected('_internal/native.dll')

    def test_binary_extension_does_not_bypass_private_key_scan(self):
        self.write('_internal/native.dll', fake_pem_header())
        self.assert_rejected('_internal/native.dll')

    def test_token_in_filename_is_redacted(self):
        prefix = 'sk-or-v1-'
        token = prefix + 'A' * 64
        self.write('docs/secret-' + token + '.txt', b'not read')
        self.assert_rejected('[redacted]', token)

    def test_existing_release_survives_rejected_rebuild(self):
        self.output.parent.mkdir(parents=True)
        self.output.write_bytes(b'previous release')
        (self.output.parent / 'SHA256SUMS').write_text('previous checksum')
        self.write('docs/example.txt', b'ghp_' + b'B' * 36)
        with self.assertRaises(packager.PackageError):
            self.package()
        self.assertEqual(self.output.read_bytes(), b'previous release')
        self.assertEqual((self.output.parent / 'SHA256SUMS').read_text(), 'previous checksum')
        self.assertEqual(list(self.output.parent.glob('.zhixian-*')), [])

    def test_cli_inputs_and_errors_never_echo_secret(self):
        prefix = 'github_pat_'
        token = prefix + 'D' * 70
        self.write('docs/example.txt', token.encode())
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = packager.main(['--input', str(self.app), '--output', str(self.output)])
        self.assertEqual(code, 1)
        self.assertIn('docs/example.txt', stderr.getvalue())
        self.assertNotIn(token, stdout.getvalue() + stderr.getvalue())
        (self.app / 'docs/example.txt').unlink()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(packager.main(['--input', str(self.app), '--output', str(self.output)]), 0)
        self.assertTrue(self.output.exists())

    def test_output_must_not_be_inside_input(self):
        with self.assertRaises(packager.PackageError):
            packager.package_portable(self.app, self.app / 'docs' / 'package.zip')


if __name__ == '__main__':
    unittest.main()
