import sys
import tempfile
import unittest
import hashlib
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_macos import audit_bundle, audit_content
from package_portable import PackageError


class MacBundleTests(unittest.TestCase):
    def test_public_tls_vector_requires_exact_hash_native_format_and_dependency_path(self):
        boundary = b'-' * 5
        public = boundary + b'BEGIN PRIVATE KEY' + boundary + b'\n' + b'QUJD' * 32 + b'\n' + boundary + b'END PRIVATE KEY' + boundary
        relative = Path('Contents/Frameworks/cv2/__dot__dylibs/libgnutls.30.dylib')
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'libgnutls.30.dylib'
            binary.write_bytes(b'\xcf\xfa\xed\xfe' + public)
            with patch('package_macos.GNUTLS_KAT_HASHES', {hashlib.sha256(public).hexdigest()}):
                audit_content(binary, relative)
                with self.assertRaises(PackageError):
                    audit_content(binary, Path('Contents/Frameworks/unreviewed.dylib'))
                binary.write_bytes(public)
                with self.assertRaises(PackageError):
                    audit_content(binary, relative)
                binary.write_bytes(b'\xcf\xfa\xed\xfe' + public + b'\x00' + public.replace(b'QUJD', b'QUJE'))
                with self.assertRaises(PackageError):
                    audit_content(binary, relative)

    def make_bundle(self, root):
        bundle = root / 'Zhixian.app'
        binary = bundle / 'Contents/MacOS/Zhixian'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'\xcf\xfa\xed\xfe' + b'synthetic native executable')
        resources = bundle / 'Contents/Resources'
        resources.mkdir()
        for name in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
            (resources / name).write_text('Synthetic public documentation')
        for name in ('docs', 'licenses'):
            (resources / name).mkdir()
        return bundle, binary, resources

    def test_private_database_missing_docs_and_embedded_token_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle, binary, resources = self.make_bundle(Path(directory))
            (resources / 'cv2/data').mkdir(parents=True)
            (resources / 'cv2/data/haarcascade_eye.xml').write_text('Synthetic public runtime data')
            audit_bundle(bundle)
            (resources / 'data').mkdir()
            with self.assertRaisesRegex(ValueError, 'private runtime'):
                audit_bundle(bundle)
            (resources / 'data').rmdir()
            private = resources / 'chatlab-index.sqlite3'
            private.touch()
            with self.assertRaisesRegex(ValueError, 'private runtime'):
                audit_bundle(bundle)
            private.unlink()
            binary.write_bytes(b'\xcf\xfa\xed\xfe' + b'gh' + b'p_' + b'x' * 32)
            with self.assertRaises(PackageError):
                audit_bundle(bundle)
            binary.write_bytes(b'\xcf\xfa\xed\xfe' + b'synthetic executable')
            (resources / 'LICENSE').unlink()
            with self.assertRaisesRegex(ValueError, 'documentation'):
                audit_bundle(bundle)

    @unittest.skipIf(sys.platform == 'win32', 'Windows hosted runners need elevation for symlinks')
    def test_internal_link_preserved_external_or_private_link_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle, _, resources = self.make_bundle(root)
            (resources / 'public-link').symlink_to('README.md')
            audit_bundle(bundle)
            (resources / 'external-link').symlink_to(root)
            with self.assertRaisesRegex(ValueError, 'outside'):
                audit_bundle(bundle)
            (resources / 'external-link').unlink()
            (resources / 'actions.sqlite3').symlink_to('README.md')
            with self.assertRaisesRegex(ValueError, 'private runtime'):
                audit_bundle(bundle)
