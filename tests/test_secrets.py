"""Publication guard tests. Every credential-shaped value here is synthetic.

Provider-shaped fixtures are built at runtime so the test source itself does
not require a broad scanner exemption. Git tests create only disposable repos.
"""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("publication_secret_guard", ROOT / "scripts/check_secrets.py")
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


def synthetic_key():
    return "sk" + "-or-v1-" + hashlib.sha256(b"ONLY_SYNTHETIC_TEST_VALUE_NEVER_ISSUED").hexdigest()


def assignment(value):
    return "api" + "_key = " + json.dumps(value)


class SecretPatternTests(unittest.TestCase):
    def test_provider_key_private_key_jwt_and_user_paths(self):
        samples = {
            "openrouter-key": synthetic_key(),
            "github-token": "gh" + "p_" + "X" * 36,
            "aws-access-key": "AK" + "IA" + "Z" * 16,
            "slack-token": "xo" + "xb-" + "1234567890-" * 4,
            "jwt-token": "ey" + "J" + "Q" * 12 + "." + "A" * 20 + "." + "B" * 20,
            "private-key": "-" * 5 + "BEGIN OPENSSH PRIVATE KEY" + "-" * 5,
            "personal-windows-path": "C:" + "\\" + "Users" + "\\" + "example-person" + "\\Documents",
            "personal-unix-path": "/" + "home" + "/example-person/private",
        }
        for expected, sample in samples.items():
            with self.subTest(expected=expected):
                found = guard.scan_text("src/example.py", "first line\n" + sample)
                self.assertIn(expected, {f.rule for f in found})
                self.assertTrue(all(f.line == 2 for f in found))

    def test_only_exact_synthetic_assignments_in_tests_are_exempt(self):
        literal = assignment("test-secret-value")
        self.assertEqual(guard.scan_text("tests/test_engine.py", literal), [])
        self.assertTrue(guard.scan_text("src/main.py", literal))
        self.assertTrue(guard.scan_text("tests/test_malicious.py", assignment("test-secret-value-but-real")))
        # A real-shaped key stays blocked even inside a test or with a comment.
        self.assertTrue(guard.scan_text("tests/test_example.py", synthetic_key() + " # secret-scan: ignore"))

    def test_placeholders_and_environment_access_are_not_secrets(self):
        for source in (assignment("YOUR_API_KEY"), assignment("<API_KEY>"), 'api_key = os.environ.get("OPENROUTER_API_KEY")'):
            self.assertEqual(guard.scan_text("src/example.py", source), [])

    def test_url_credentials_detected(self):
        for text in ("https://" + "person:synthetic-password@example.invalid/api", "https://example.invalid/api?" + "token=" + "synthetic-value-123"):
            self.assertIn("credential-in-url", {f.rule for f in guard.scan_text("README.md", text)})

    def test_url_fixture_exception_is_exact_path_and_exact_value(self):
        toy_url = "https://" + "user:" + "password@example.com/api"
        self.assertEqual(guard.scan_text("tests/test_store.py", json.dumps(toy_url)), [])
        self.assertTrue(guard.scan_text("src/main.py", json.dumps(toy_url)))
        self.assertTrue(guard.scan_text("tests/test_store.py", json.dumps(toy_url.replace("password", "real-secret-value"))))

    def test_packaged_selftest_exception_is_exact(self):
        toy = assignment("synthetic-diagnostic-only")
        self.assertEqual(guard.scan_text("src/desk/selftest.py", toy), [])
        self.assertTrue(guard.scan_text("src/main.py", toy))

    def test_output_never_contains_matched_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "src").mkdir()
            secret = synthetic_key()
            (root / "src/example.py").write_text(assignment(secret), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = guard.main(["--tree", str(root)])
            self.assertEqual(code, 1)
            self.assertNotIn(secret, out.getvalue())
            self.assertIn("src/example.py:1:", out.getvalue())
            self.assertIn("openrouter-key", out.getvalue())
            self.assertNotIn("api_key =", out.getvalue())

    def test_filename_containing_key_is_redacted(self):
        secret = synthetic_key()
        displayed = guard.display_path("src/" + secret + ".py")
        self.assertNotIn(secret, displayed)
        self.assertIn("sensitive-path", displayed)
        self.assertEqual(guard.scan_blob("src/" + secret + ".py", b"print('clean')")[0].rule, "sensitive-filename")


class SourceBoundaryTests(unittest.TestCase):
    def test_tree_never_reads_runtime_directories_or_state_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for private in ("data", "work", "outputs", ".venv", ".git"):
                (root / private).mkdir()
                (root / private / "private.py").write_text(synthetic_key(), encoding="utf-8")
            (root / "src").mkdir()
            (root / "src/main.py").write_text("print('public')", encoding="utf-8")
            (root / "src/credentials.json").write_text(synthetic_key(), encoding="utf-8")
            (root / ".env").write_text(synthetic_key(), encoding="utf-8")
            real_read = Path.read_bytes
            reads = []
            def recording(path):
                reads.append(path.relative_to(root).as_posix())
                return real_read(path)
            with patch.object(Path, "read_bytes", recording):
                findings, count = guard.scan_tree(root)
            self.assertEqual(findings, [])
            self.assertEqual(count, 1)
            self.assertEqual(reads, ["src/main.py"])

    def test_explicit_forbidden_file_blocked_without_read(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(Path, "read_bytes", side_effect=AssertionError("Must not read")):
            findings, count = guard.scan_tree(Path(directory).resolve(), ["data/credentials.json", "outputs/live-capture-check.json"])
            self.assertEqual(count, 0)
            self.assertEqual({f.rule for f in findings}, {"private-runtime-file"})

    def test_state_files_anywhere_and_release_archives_are_blocked(self):
        for name in ("src/config.json", "tests/knowledge.json", "vendor/history.json", "ui/credentials.backup.json", "src/.env.local", "src/id_ed25519", "docs/live-capture-check.json"):
            self.assertEqual(guard.path_policy(name), "private-runtime-file", name)
        for name in ("docs/release.zip", "ui/live-screenshot.png", "vendor/program.exe"):
            self.assertEqual(guard.path_policy(name), "unreviewed-binary-or-artifact", name)

    def test_reviewed_asset_requires_exact_hash_and_path(self):
        raw = b"synthetic image content"
        with patch.dict(guard.REVIEWED_ASSETS, {"ui/icon.png": hashlib.sha256(raw).hexdigest()}):
            self.assertEqual(guard.scan_blob("ui/icon.png", raw), [])
            self.assertEqual(guard.scan_blob("ui/icon.png", raw + b"changed")[0].rule, "reviewed-asset-hash-changed")
            self.assertEqual(guard.scan_blob("ui/private.png", raw)[0].rule, "unreviewed-binary-or-artifact")

    def test_unsupported_encoding_and_oversize_fail_closed(self):
        self.assertEqual(guard.scan_blob("src/file.py", b"\x00\x01")[0].rule, "non-text-source")
        self.assertEqual(guard.scan_blob("src/file.py", b"\xff\xf0\x8a")[0].rule, "non-text-source")
        self.assertTrue(guard.scan_blob("src/file.py", synthetic_key().encode("utf-16")))
        with patch.object(guard, "MAX_BYTES", 4):
            self.assertEqual(guard.scan_blob("src/file.py", b"12345")[0].rule, "source-too-large-to-review")

    def test_parent_traversal_refused(self):
        with self.assertRaises(guard.ScanError):
            guard.normalize_path("../data/credentials.json")

    def test_explicit_path_through_link_ancestor_is_not_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "src/link").mkdir(parents=True)
            (root / "src/link/example.py").write_text(synthetic_key(), encoding="utf-8")
            original = Path.is_symlink
            def is_link(path):
                return path == root / "src/link" or original(path)
            with patch.object(Path, "is_symlink", is_link), patch.object(Path, "read_bytes", side_effect=AssertionError("Must not follow linked parent")):
                findings, count = guard.scan_tree(root, ["src/link/example.py"])
            self.assertEqual(count, 0)
            self.assertEqual(findings[0].rule, "source-symlink-or-junction")

    def test_symlink_never_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "src").mkdir()
            link = root / "src/link.py"
            try:
                link.symlink_to(root / "data/private.py")
            except OSError:
                self.skipTest("Symlink creation unavailable")
            findings, _ = guard.scan_tree(root)
            self.assertIn("source-symlink-or-junction", {f.rule for f in findings})

    def test_noncanonical_root_uses_canonical_read_paths(self):
        with tempfile.TemporaryDirectory(prefix="zhixian-long-source-path-") as directory:
            root = Path(directory).resolve()
            (root / "src").mkdir()
            (root / "src/main.py").write_text("print('public')", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data/private.py").write_text(synthetic_key(), encoding="utf-8")
            # Exercise a noncanonical spelling on every platform. Where NTFS
            # supplies an 8.3 alias, also use the same spelling seen on CI.
            alias = root / "src" / ".."
            if os.name == "nt":
                import ctypes
                get_short = ctypes.windll.kernel32.GetShortPathNameW
                get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
                get_short.restype = ctypes.c_uint
                needed = get_short(str(root), None, 0)
                if needed:
                    buffer = ctypes.create_unicode_buffer(needed)
                    written = get_short(str(root), buffer, needed)
                    if 0 < written < needed:
                        alias = Path(buffer.value) / "src" / ".."
            self.assertEqual(alias.resolve(), root)
            real_read, reads = Path.read_bytes, []
            def recording(path):
                reads.append(path.relative_to(root).as_posix())
                return real_read(path)
            with patch.object(Path, "read_bytes", recording):
                findings, count = guard.scan_tree(alias)
            self.assertEqual(findings, [])
            self.assertEqual(count, 1)
            self.assertEqual(reads, ["src/main.py"])


@unittest.skipUnless(shutil.which("git"), "Git unavailable")
class GitBlobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zhixian-publication-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        # Verify the enclosing Git boundary before creating the disposable repo.
        probe = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--show-toplevel"], capture_output=True)
        if probe.returncode == 0:
            self.assertNotEqual(Path(os.fsdecode(probe.stdout).strip()).resolve(), Path.home().resolve())
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True, capture_output=True)
        self.run_git("config", "user.name", "Synthetic Test")
        self.run_git("config", "user.email", "synthetic@example.invalid")
        (self.root / "src").mkdir()

    def run_git(self, *args):
        root = subprocess.run(["git", "-C", str(self.root), "rev-parse", "--show-toplevel"], check=True, capture_output=True)
        self.assertEqual(Path(os.fsdecode(root.stdout).strip()).resolve(), self.root)
        self.assertNotEqual(self.root, Path.home().resolve())
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True).stdout

    def test_staged_blob_not_clean_working_copy_is_scanned(self):
        path = self.root / "src/main.py"
        path.write_text(assignment(synthetic_key()), encoding="utf-8")
        self.run_git("add", "src/main.py")
        path.write_text("print('clean working copy')", encoding="utf-8")
        findings, count = guard.GitSource(self.root).scan()
        self.assertEqual(count, 1)
        self.assertIn("openrouter-key", {f.rule for f in findings})

    def test_deleted_secret_in_history_is_still_detected(self):
        path = self.root / "src/main.py"
        path.write_text(assignment(synthetic_key()), encoding="utf-8")
        self.run_git("add", "src/main.py")
        self.run_git("commit", "-qm", "Synthetic historical fixture")
        path.write_text("print('now clean')", encoding="utf-8")
        self.run_git("add", "src/main.py")
        self.run_git("commit", "-qm", "Remove synthetic fixture")
        self.assertEqual(guard.GitSource(self.root).scan()[0], [])
        findings, _ = guard.GitSource(self.root).scan(history=True)
        self.assertIn("openrouter-key", {f.rule for f in findings})

    def test_forced_staged_private_file_is_not_read(self):
        (self.root / "data").mkdir()
        (self.root / "data/credentials.json").write_text(synthetic_key(), encoding="utf-8")
        self.run_git("add", "-f", "data/credentials.json")
        git = guard.GitSource(self.root)
        original = git.command
        calls = []
        def command(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs)
        with patch.object(git, "command", command):
            findings, count = git.scan()
        self.assertEqual(count, 0)
        self.assertEqual(findings[0].rule, "private-runtime-file")
        self.assertFalse(any(c[0] == "cat-file" for c in calls))

    def test_nested_wrong_root_is_refused(self):
        with self.assertRaises(guard.ScanError):
            guard.GitSource(self.root / "src")

    def test_gitignore_blocks_runtime_and_allows_source_assets(self):
        shutil.copyfile(ROOT / ".gitignore", self.root / ".gitignore")
        blocked = ["data/config.json", "work/test.py", "outputs/result.md", ".venv/file", ".env.local", "src/credentials.json", "src/knowledge.json", "src/history.json", "src/config.json", "docs/private.png", "docs/live-capture-check.json", "docs/release.zip"]
        for path in blocked:
            result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", path], capture_output=True)
            self.assertEqual(result.returncode, 0, path)
        for path in ("src/main.py", "tests/test_secrets.py", "ui/icon.png", "vendor/jev-chat-windows/docs/ui_home.png"):
            result = subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", path], capture_output=True)
            self.assertEqual(result.returncode, 1, path)


if __name__ == "__main__":
    unittest.main()
