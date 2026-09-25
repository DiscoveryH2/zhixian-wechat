#!/usr/bin/env python3
"""Fail-closed source publication check. Python standard library only.

Examples (run from repository root):
  python scripts/check_secrets.py --tree .
  python scripts/check_secrets.py --staged
  python scripts/check_secrets.py --history
  python scripts/check_secrets.py --tree . --staged --history
  python scripts/check_secrets.py --files src/core/client.py README.md

--tree walks only the explicit public source boundary below; runtime/private
directories are pruned WITHOUT reading their contents. --staged checks every
index blob (not the working copy). --history checks all locally reachable commit
trees, including deleted files. Reflogs, dangling objects and remote-only refs
are outside --history; this does not rewrite existing history.

Source-only policy: release ZIPs/executables/models and unreviewed images are
rejected, not unpacked. Distribution archives require a separate release review.
Approved visual assets require both an exact path and the pinned SHA256.
This is a pattern-based guard, not a guarantee that arbitrary prose is public;
review conversations, contact names and screenshots manually before publication.

Output contains ONLY path, rule and location. Never print matched strings,
source excerpts, raw exception messages, Git stderr, or credentials.
Exit codes: 0 clean, 1 findings, 2 scan could not finish reliably.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


PUBLIC_DIRS = frozenset({"src", "ui", "tests", "scripts", "vendor", "docs", ".github", ".githooks"})
PUBLIC_ROOT_FILES = frozenset({
    ".gitignore", ".gitattributes", ".editorconfig", "README.md", "LICENSE", "LICENSE.md", "LICENSE.txt",
    "NOTICE", "NOTICE.md", "THIRD_PARTY_NOTICES.md", "CONTRACT.md", "SECURITY.md", "CONTRIBUTING.md",
    "CHANGELOG.md", "requirements.txt", "requirements-lock.txt", "pyproject.toml", "setup.cfg",
    "Zhixian.spec", "启动知弦.cmd",
})
PRIVATE_DIRS = frozenset({"data", "work", "outputs", ".venv", "venv", ".git", "__pycache__", ".pytest_cache",
                          ".mypy_cache", ".ruff_cache", ".idea", ".vscode", "node_modules", "build", "dist"})
TEXT_SUFFIXES = frozenset({".py", ".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".cfg", ".ini",
                           ".ps1", ".cmd", ".bat", ".sh", ".html", ".css", ".js", ".ts", ".tsx", ".jsx", ".svg",
                           ".xml", ".kt", ".kts", ".gradle", ".spec", ".lock", ".properties"})
MAX_BYTES = 8 * 1024 * 1024
MAX_REVIEWED_ASSET_BYTES = 32 * 1024 * 1024
REVIEWED_ASSETS = {
    "ui/icon.ico": "5b9f0f1223d1d560ab3348f0cc5a47fc19e4d51b80c1ba9a7e9ee866c4c293f0",
    "ui/icon.png": "9c15e704220a0cf215a0ea5e0135cd4d81f32e6f25fe2f3e62cb2bb9de255486",
    "ui/fonts/NotoSansSC-Variable.ttf": "a3041811a78c361b1de50f953c805e0244951c21c5bd412f7232ef0d899af0da",
    # Both versions were separately reviewed synthetic renders. Retain the
    # previous hashes because --history also scans the earlier public commit.
    "docs/images/workspace.png": ("8f1b9d81b18c1b0d9e6ef38a8b58b090b8d1014cfcaf37f891454da979f87bbc",
                                  "37339b319ef8bf581700bf1d7c78129ef27a42e1e53aa9db41a041530c2f5ea9"),
    "docs/images/compact.png": ("2229a5d60c86fe48b8319f6fdb9c2b76632b4654a88038c6042a71055e88652b",
                                "2abf4924fd61a710de8f4d2c9b417eddbb639ea68f156ef7715470b6ab175078"),
    "docs/images/intro.png": "28e2dd0bed30120175e8caac10192663f1e954a31c97fa85f302dd742e825745",
    "docs/images/catalogue.png": "bf9dd39408256b49e128cbdb68a62daf4354ee3e4316ef0e34f75748cb6094dd",
    "docs/images/moments.png": "b724624feb9313ba726136e17527018ee6f79d2409bdde4eefac5820e87af99e",
    "docs/images/auto-reply.png": "b38edc44560a8bb9e0f9b8e78aba699923402e90f5013b74b9c90414e52b2d63",
    "docs/images/agent.png": "f7fbd12aee41c922dc6bcb4c1ccfb517448e73fb91fbeb142ba5464c388eadb0",
    "vendor/jev-chat-windows/docs/icon.ico": "fe7e379a97d323dddc1b3fac66d80fd9113ad1c08b0216d886cd2ba18b6788a9",
    "vendor/jev-chat-windows/docs/ui_home.png": "ef24f248cbe6cb8895a9a363845d857c80410d21c66910c52eea6ab2383ca9aa",
    "vendor/jev-chat-windows/docs/ui_settings.png": "83bc9bf2df279de0a29d908fa488632d2d085cb88b72a668b3458ee93cc303de",
    "vendor/jev-chat-windows/docs/ui_toggle_off.png": "30e5cb2e64bf8bee97eb907c949186f68bd4706249126d1b2c54b3697d886ece",
}
# Exact obviously synthetic words used by the existing tests. These exemptions
# apply only to generic-assignment rules in test files, never provider-token or
# private-key patterns. No skip-line / ignore-directory annotations are accepted.
SYNTHETIC_FIXTURES = frozenset({"test-secret-value", "separate-test-key", "synthetic-smoke-only", "synthetic-test-key", "synthetic-test-only",
                                 "synthetic-only", "test-api-key", "test-reply-key", "test-weflow-token"})
SYNTHETIC_SOURCE_FIXTURES = {"src/desk/selftest.py": frozenset({"synthetic-diagnostic-only"})}
SYNTHETIC_URL_FIXTURES = {
    "tests/test_store.py": ("https://" + "user:" + "password@example.com/api",),
    "tests/test_capture.py": ("http://" + "x:y@localhost",),
}

RULES = (
    ("openrouter-key", re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{12,}\b")),
    ("provider-secret-key", re.compile(r"\bsk-(?:(?:proj|ant|live|test|ts)-)?[A-Za-z0-9_-]{24,}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b")),
    ("jwt-token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("private-key", re.compile("-" * 5 + r"BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY" + "-" * 5 + "|" + "-" * 5 + "BEGIN PGP PRIVATE KEY BLOCK" + "-" * 5)),
    ("personal-windows-path", re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+(?!Public(?:[\\/]|\b)|Default(?:[\\/]|\b)|<|%|\$)[^\\/\s\"'<>]+")),
    ("personal-unix-path", re.compile(r"(?<![A-Za-z0-9])/(?:Users|home)/(?!(?:<|\$|\{|USER\b|username\b))[^/\s\"'<>]+")),
)
ASSIGNMENT = re.compile(
    r"(?i)[\"']?(?:[A-Z0-9_]*API[_-]?KEY|[A-Z0-9_]*ACCESS[_-]?TOKEN|[A-Z0-9_]*AUTH[_-]?TOKEN|"
    r"[A-Z0-9_]*CLIENT[_-]?SECRET|[A-Z0-9_]*PASSWORD|[A-Z0-9_]*SECRET[_-]?KEY|TOKEN)[\"']?"
    r"\s*[:=]\s*[\"'](?P<value>[^\"'\r\n]{8,})[\"']"
)
URL_CREDENTIALS = re.compile(r"(?i)https?://[^\s/\"'<>]+:[^\s/\"'<>]+@|[?&](?:api[_-]?key|access[_-]?token|token|password|secret)=[^&\s\"'<>]{8,}")
PLACEHOLDER = re.compile(r"(?i)^(?:<[^>]+>|\$\{[^}]+\}|\$[A-Z_]+|YOUR_[A-Z_]+|REPLACE[_-][A-Z_]+|\*+|\.{3,})$")
PRIVATE_FILE = re.compile(r"(?i)^(?:credentials?|history|knowledge|config|secrets?)(?:[._-].*)?$")
STATE_EXTS = frozenset({".json", ".jsonl", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".db", ".sqlite", ".sqlite3", ".txt", ".log", ".dat", ".bin", ".bak", ".csv", ".xlsx", ".xls"})


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str
    line: int = 0
    column: int = 0


class ScanError(Exception):
    """Only fixed messages are allowed here."""


def normalize_path(value: str) -> str:
    value = value.replace("\\", "/")
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or not p.parts or any(":" in part for part in p.parts):
        raise ScanError("Invalid relative source path.")
    return str(p)


def private_path(path: str) -> bool:
    p = PurePosixPath(path)
    if any(part.lower() in PRIVATE_DIRS for part in p.parts):
        return True
    name = p.name.lower()
    if name == ".env" or name.startswith(".env.") or name.startswith(("id_rsa", "id_ed25519", "id_ecdsa")):
        return True
    if name in {"api_key.txt", "apikey.txt", "token.txt"} or p.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".kdbx", ".keystore", ".db", ".sqlite", ".sqlite3", ".har", ".dmp", ".log", ".tmp", ".bak", ".pyc", ".pyo"}:
        return True
    if PRIVATE_FILE.match(name) and (p.suffix.lower() in STATE_EXTS or ".json" in name or not p.suffix):
        return True
    return bool(re.search(r"(?i)(?:report|self[-_]?test|capture[-_]check|live[-_]check|验证记录|实测记录|测试报告).*\.(?:json|jsonl|html|csv|png|jpg|pdf|md|txt)$", name))


def allowed_source(path: str) -> bool:
    p = PurePosixPath(path)
    return (len(p.parts) == 1 and path in PUBLIC_ROOT_FILES) or (len(p.parts) > 1 and p.parts[0] in PUBLIC_DIRS)


def path_policy(path: str) -> str | None:
    if any(pattern.search(path) for _, pattern in RULES) or URL_CREDENTIALS.search(path):
        return "sensitive-filename"
    if private_path(path):
        return "private-runtime-file"
    if not allowed_source(path):
        return "outside-public-allowlist"
    p = PurePosixPath(path)
    if path in REVIEWED_ASSETS:
        return None
    if p.suffix.lower() in TEXT_SUFFIXES or p.name.startswith(("LICENSE", "NOTICE")) or p.name in {".gitignore", ".gitattributes", ".editorconfig", "pre-commit"}:
        return None
    return "unreviewed-binary-or-artifact"


def _fixture(path: str, value: str) -> bool:
    return (value in SYNTHETIC_FIXTURES and (path.startswith("tests/test_") or path == "scripts/smoke_bridge.py")) or value in SYNTHETIC_SOURCE_FIXTURES.get(path, ())


def scan_text(path: str, text: str) -> list[Finding]:
    findings = []
    def add(rule, start):
        line = text.count("\n", 0, start) + 1
        column = start - text.rfind("\n", 0, start)
        findings.append(Finding(path, rule, line, column))
    for rule, pattern in RULES:
        for match in pattern.finditer(text):
            add(rule, match.start())
    for match in ASSIGNMENT.finditer(text):
        value = match.group("value").strip()
        if PLACEHOLDER.fullmatch(value) or _fixture(path, value):
            continue
        # Variable templates/code are not literal credentials. Whitespace prose
        # is still checked by the provider-token rules above.
        if any(c.isspace() for c in value) or value.startswith(("http://", "https://")):
            continue
        add("literal-credential-assignment", match.start("value"))
    for match in URL_CREDENTIALS.finditer(text):
        if any(text.startswith(fixture, match.start()) and text[match.start() + len(fixture):match.start() + len(fixture) + 1] in ("'", '"', "", " ", "\n") for fixture in SYNTHETIC_URL_FIXTURES.get(path, ())):
            continue
        add("credential-in-url", match.start())
    return list(dict.fromkeys(findings))


def scan_blob(path: str, raw: bytes) -> list[Finding]:
    policy = path_policy(path)
    if policy:
        return [Finding(path, policy)]
    if len(raw) > _size_limit(path):
        return [Finding(path, "source-too-large-to-review")]
    if path in REVIEWED_ASSETS:
        pinned = REVIEWED_ASSETS[path]
        accepted = (pinned,) if isinstance(pinned, str) else pinned
        return [] if hashlib.sha256(raw).hexdigest() in accepted else [Finding(path, "reviewed-asset-hash-changed")]
    try:
        # Explicit BOMs are supported; unknown binary encodings fail closed.
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = raw.decode("utf-16")
        else:
            text = raw.decode("utf-8-sig")
    except UnicodeError:
        return [Finding(path, "non-text-source")]
    if "\x00" in text:
        return [Finding(path, "non-text-source")]
    return scan_text(path, text)


def _size_limit(path: str) -> int:
    # Only individually reviewed, hash-pinned assets get the larger ceiling.
    return MAX_REVIEWED_ASSET_BYTES if path in REVIEWED_ASSETS else MAX_BYTES


def scan_tree(root: Path, files: list[str] | None = None) -> tuple[list[Finding], int]:
    root = root.resolve()
    findings, count = [], 0
    def inspect(path: str):
        nonlocal count
        physical = root / path
        # Never dereference a link or junction into a user's private directory.
        ancestors = [physical]
        parent = physical.parent
        while parent != root and parent.is_relative_to(root):
            ancestors.append(parent)
            parent = parent.parent
        if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in ancestors):
            findings.append(Finding(path, "source-symlink-or-junction"))
            return
        policy = path_policy(path)
        if policy:
            findings.append(Finding(path, policy))
            return
        if not physical.is_file():
            findings.append(Finding(path, "not-a-regular-source-file"))
            return
        resolved = physical.resolve()
        if not resolved.is_relative_to(root):
            findings.append(Finding(path, "source-outside-root"))
            return
        if physical.stat().st_size > _size_limit(path):
            findings.append(Finding(path, "source-too-large-to-review"))
            return
        findings.extend(scan_blob(path, physical.read_bytes()))
        count += 1
    if files is not None:
        for value in files:
            path = normalize_path(value)
            # An explicitly named forbidden file is rejected without opening it.
            inspect(path)
        return findings, count
    for current, dirs, names in os.walk(root, followlinks=False):
        base = Path(current)
        retained = []
        for name in dirs:
            directory = base / name
            rel = directory.relative_to(root).as_posix()
            if name.lower() in PRIVATE_DIRS or name.lower().startswith(".env"):
                continue
            if directory.is_symlink() or (hasattr(directory, "is_junction") and directory.is_junction()):
                findings.append(Finding(rel, "source-symlink-or-junction"))
                continue
            if base == root and name not in PUBLIC_DIRS:
                findings.append(Finding(rel, "outside-public-allowlist"))
                continue
            retained.append(name)
        dirs[:] = retained
        for name in names:
            path = (base / name).relative_to(root).as_posix()
            if private_path(path):
                # Private working files are expected locally, but forbidden if
                # explicitly selected, staged or committed.
                continue
            inspect(path)
    return findings, count


class GitSource:
    def __init__(self, root: Path):
        requested = root.resolve()
        # First Git operation always establishes the repository boundary.
        result = subprocess.run(["git", "-C", str(requested), "rev-parse", "--show-toplevel"], capture_output=True, timeout=20)
        if result.returncode:
            raise ScanError("Git root could not be verified; initialize the intended project first.")
        self.root = Path(os.fsdecode(result.stdout).strip()).resolve()
        if self.root == Path.home().resolve() or self.root != requested:
            raise ScanError("Refusing a home-directory or unexpected parent Git repository.")

    def command(self, *args: str, stdin: bytes | None = None) -> bytes:
        result = subprocess.run(["git", "-C", str(self.root), *args], input=stdin, capture_output=True, timeout=120)
        if result.returncode:
            raise ScanError("A Git read operation failed; scan is incomplete.")
        return result.stdout

    def staged(self):
        for entry in self.command("ls-files", "--stage", "-z").split(b"\x00"):
            if not entry:
                continue
            metadata, path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.split()
            if stage != b"0":
                raise ScanError("Unmerged index entries must be resolved before publication.")
            yield os.fsdecode(path), mode.decode(), oid.decode()

    def history(self):
        commits = self.command("rev-list", "--all").splitlines()
        seen = set()
        for commit in commits:
            for entry in self.command("ls-tree", "-r", "-z", commit.decode("ascii")).split(b"\x00"):
                if not entry:
                    continue
                metadata, path = entry.split(b"\t", 1)
                mode, kind, oid = metadata.split()
                item = (os.fsdecode(path), mode.decode(), oid.decode())
                if item not in seen:
                    seen.add(item)
                    yield item

    def scan(self, history=False) -> tuple[list[Finding], int]:
        findings, count = [], 0
        cache = {}
        for path, mode, oid in self.history() if history else self.staged():
            path = normalize_path(path)
            if mode in {"120000", "160000"}:
                findings.append(Finding(path, "source-symlink-or-submodule"))
                continue
            policy = path_policy(path)
            if policy:
                # Do not read credentials or private data even from Git blobs.
                findings.append(Finding(path, policy))
                continue
            size = int(self.command("cat-file", "-s", oid))
            if size > _size_limit(path):
                findings.append(Finding(path, "source-too-large-to-review"))
                continue
            if oid not in cache:
                cache[oid] = self.command("cat-file", "blob", oid)
            findings.extend(scan_blob(path, cache[oid]))
            count += 1
        return findings, count


def display_path(path: str) -> str:
    # A malicious filename can itself contain a token. Hide the entire name
    # rather than leaking the value through the otherwise-safe path report.
    if any(pattern.search(path) for _, pattern in RULES) or URL_CREDENTIALS.search(path):
        return "[sensitive-path:" + hashlib.sha256(path.encode(errors="replace")).hexdigest()[:12] + "]"
    return json.dumps(path, ensure_ascii=True)[1:-1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", default=".", help="Exact Git/project root for staged/history/files modes")
    parser.add_argument("--tree", nargs="?", const=".", help="Scan public source files without entering private directories")
    parser.add_argument("--staged", action="store_true", help="Scan all index blobs, including staged edits")
    parser.add_argument("--history", action="store_true", help="Scan all locally reachable committed source blobs")
    parser.add_argument("--files", nargs="+", help="Explicit relative public source files; private paths are refused")
    args = parser.parse_args(argv)
    if not (args.tree is not None or args.staged or args.history or args.files):
        args.tree = args.root
    findings, count = [], 0
    try:
        if args.tree is not None:
            if not Path(args.tree).is_dir():
                raise ScanError("Source tree is not an existing directory.")
            found, n = scan_tree(Path(args.tree))
            findings.extend(found)
            count += n
        if args.files:
            found, n = scan_tree(Path(args.root), args.files)
            findings.extend(found)
            count += n
        if args.staged or args.history:
            git = GitSource(Path(args.root))
            for history in ([False] if args.staged else []) + ([True] if args.history else []):
                found, n = git.scan(history=history)
                findings.extend(found)
                count += n
    except (ScanError, OSError, ValueError, subprocess.SubprocessError):
        # No raw exception: it may contain a path, credential, or Git stderr.
        print("ERROR: source scan could not finish reliably; publication is blocked.")
        return 2
    findings = sorted(set(findings), key=lambda f: (f.path, f.line, f.column, f.rule))
    for finding in findings:
        print(f"BLOCK {display_path(finding.path)}:{finding.line}:{finding.column} [{finding.rule}]")
    print(f"Source scan: {count} reviewed file versions; {len(findings)} finding(s).")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
