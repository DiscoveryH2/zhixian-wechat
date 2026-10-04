import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_macos import audit_bundle
from package_portable import PackageError


class MacBundleTests(unittest.TestCase):
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
            audit_bundle(bundle)
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
