"""Private, per-conversation storage for Work Agent turns and artifacts."""
from __future__ import annotations

import functools
import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from PIL import Image, ImageOps


MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_FILES_PER_TURN = 20
MAX_FILES_PER_SESSION = 100
MAX_SESSION_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_TEXT = 120_000
MAX_ZIP_ENTRIES = 200
MAX_ZIP_TOTAL = 20 * 1024 * 1024
MAX_IMAGE_SIZE = 2 * 1024 * 1024
MAX_IMAGE_DIMENSION = 1600
MAX_IMAGE_PIXELS = 40_000_000
MAX_ARTIFACTS = 3
MAX_ARTIFACT_SIZE = 100_000
TEXT_EXTENSIONS = {".txt", ".md", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css", ".json", ".csv", ".xml", ".yaml", ".yml", ".toml", ".ini", ".log", ".sql", ".sh", ".bat", ".c", ".cpp", ".h", ".java", ".go", ".rs"}
SUPPORTED = TEXT_EXTENSIONS | {".pdf", ".docx", ".xlsx", ".pptx", ".png", ".jpg", ".jpeg", ".webp"}


class WorkAgentStoreError(ValueError):
    """Invalid input, inaccessible data, or unsupported attachment."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


def _synchronized(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class WorkAgentStore:
    """Store isolated sessions beneath a caller-supplied private data root."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        with self._locks_guard:
            self._lock = self._locks.setdefault(str(self.root), threading.RLock())

    def _session_dir(self, sid: str) -> Path:
        try:
            parsed = uuid.UUID(str(sid))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WorkAgentStoreError("Invalid session id") from exc
        if str(parsed) != str(sid).lower():
            raise WorkAgentStoreError("Invalid session id")
        path = (self.root / str(parsed)).resolve()
        if path.parent != self.root:
            raise WorkAgentStoreError("Session path escapes store")
        if not path.is_dir():
            raise WorkAgentStoreError("Session not found")
        return path

    @staticmethod
    def _read_manifest(directory: Path) -> dict[str, Any]:
        try:
            data = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkAgentStoreError("Session manifest is unavailable") from exc
        if not isinstance(data, dict):
            raise WorkAgentStoreError("Invalid session manifest")
        return data

    @staticmethod
    def _write_manifest(directory: Path, data: dict[str, Any]) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, directory / "manifest.json")
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    @_synchronized
    def create_session(self) -> dict[str, Any]:
        sid = _new_id()
        directory = self.root / sid
        directory.mkdir(mode=0o700)
        (directory / "attachments").mkdir()
        (directory / "artifacts").mkdir()
        now = _now()
        manifest = {"id": sid, "title": "新任务", "created_at": now, "updated_at": now, "messages": [], "attachments": [], "artifacts": []}
        self._write_manifest(directory, manifest)
        return self._public(manifest)

    @_synchronized
    def list_sessions(self) -> list[dict[str, Any]]:
        result = []
        for path in self.root.iterdir():
            if path.is_dir():
                try:
                    result.append(self._public(self._read_manifest(path), summary=True))
                except WorkAgentStoreError:
                    continue
        return sorted(result, key=lambda item: item.get("updated_at", ""), reverse=True)

    def get_session(self, sid: str) -> dict[str, Any]:
        return self._public(self._read_manifest(self._session_dir(sid)))

    @staticmethod
    def _public(manifest: dict[str, Any], summary: bool = False) -> dict[str, Any]:
        messages = [{k: v for k, v in m.items() if k in {"id", "created_at", "role", "content", "attachment_ids", "sources", "trace", "artifact_ids"}} for m in manifest.get("messages", [])]
        result = {"id": manifest["id"], "title": manifest.get("title", "新任务"), "created_at": manifest["created_at"], "updated_at": manifest["updated_at"]}
        if summary:
            result.update({"message_count": len(messages), "preview": next((m["content"][:160] for m in reversed(messages) if m.get("role") == "assistant" and m.get("content")), "")})
            return result
        result.update({"messages": messages,
                       "attachments": [{"id": a["id"], "name": a["name"], "mime_type": a.get("mime_type", a.get("mime")), "kind": a["kind"], "size": a["size"]} for a in manifest.get("attachments", [])],
                       "artifacts": [{"id": a["id"], "name": a["name"], "mime_type": a.get("mime_type", a.get("mime")), "size": a["size"]} for a in manifest.get("artifacts", [])]})
        return result

    @staticmethod
    def _safe_name(path: str) -> str:
        # 同时按 / 和 \ 取最后一段：macOS 上 Path 不把反斜杠当分隔符，"..\\..\\x" 会残留 ".."。
        name = re.split(r"[\\/]", str(path))[-1]
        name = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", name).strip(" .")
        return (name or "attachment")[:180]

    @_synchronized
    def attach_files(self, sid: str, paths: list[str]) -> list[dict[str, Any]]:
        directory = self._session_dir(sid)
        manifest = self._read_manifest(directory)
        if not isinstance(paths, list) or len(paths) > MAX_FILES_PER_TURN:
            raise WorkAgentStoreError(f"Select at most {MAX_FILES_PER_TURN} files")
        if len(manifest.get("attachments", [])) + len(paths) > MAX_FILES_PER_SESSION:
            raise WorkAgentStoreError("Conversation attachment limit reached")
        attachments = []
        created = []
        try:
            for raw in paths:
                source = Path(raw).expanduser()
                if not source.is_file():
                    raise WorkAgentStoreError("Selected file is unavailable")
                size = source.stat().st_size
                if size > MAX_FILE_SIZE:
                    raise WorkAgentStoreError("File exceeds the 20 MB limit")
                suffix = source.suffix.lower()
                if suffix not in SUPPORTED:
                    raise WorkAgentStoreError(f"Unsupported file type: {suffix or 'unknown'}")
                raw_bytes = source.read_bytes()
                aid = _new_id()
                name = self._safe_name(source.name)
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                kind = "image" if suffix in {".png", ".jpg", ".jpeg", ".webp"} else "text"
                stored = raw_bytes
                if kind == "image":
                    stored, mime, name = self._normalize_image(raw_bytes, suffix, name)
                    size = len(stored)
                else:
                    self._extract_text(raw_bytes, suffix)
                target = directory / "attachments" / f"{aid}{suffix if kind == 'text' else ('.png' if mime == 'image/png' else '.jpg')}"
                target.write_bytes(stored)
                created.append(target)
                attachments.append({"id": aid, "name": name, "mime_type": mime, "kind": kind, "size": size,
                                    "_file": target.name, "_source_type": suffix})
            if sum(a.get("size", 0) for a in manifest.get("attachments", [])) + sum(a["size"] for a in attachments) > MAX_SESSION_BYTES:
                raise WorkAgentStoreError("Conversation attachment storage limit reached")
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        manifest.setdefault("attachments", []).extend(attachments)
        manifest["updated_at"] = _now()
        self._write_manifest(directory, manifest)
        return [{k: v for k, v in a.items() if not k.startswith("_")} for a in attachments]

    @staticmethod
    def _normalize_image(raw: bytes, suffix: str, name: str) -> tuple[bytes, str, str]:
        try:
            with Image.open(io.BytesIO(raw)) as img:
                if getattr(img, "is_animated", False):
                    raise WorkAgentStoreError("Animated images are unsupported")
                if img.width * img.height > MAX_IMAGE_PIXELS:
                    raise WorkAgentStoreError("Image exceeds the safe pixel limit")
                img.load()
                img = ImageOps.exif_transpose(img)
                img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
                if img.mode not in ("RGB", "L"):
                    if "A" in img.getbands():
                        rgba = img.convert("RGBA")
                        canvas = Image.new("RGB", rgba.size, "white")
                        canvas.paste(rgba, mask=rgba.getchannel("A"))
                        img = canvas
                    else:
                        img = img.convert("RGB")
                fmt = "PNG" if suffix == ".png" else "JPEG"
                name = f"{Path(name).stem}{'.png' if fmt == 'PNG' else '.jpg'}"
                if fmt == "JPEG" and img.mode == "L":
                    pass
                output = io.BytesIO()
                img.save(output, format=fmt, optimize=True, quality=88)
                data = output.getvalue()
                if len(data) > MAX_IMAGE_SIZE:
                    output = io.BytesIO()
                    img.save(output, format=fmt, optimize=True, quality=72)
                    data = output.getvalue()
                if len(data) > MAX_IMAGE_SIZE:
                    raise WorkAgentStoreError("Normalized image exceeds the 2 MB limit")
                return data, ("image/png" if fmt == "PNG" else "image/jpeg"), name
        except WorkAgentStoreError:
            raise
        except Exception as exc:
            raise WorkAgentStoreError("Invalid or unsupported image") from exc

    @staticmethod
    def _zip_read(raw: bytes, expected: str | None = None) -> dict[str, bytes]:
        try:
            archive = zipfile.ZipFile(io.BytesIO(raw))
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES or sum(i.file_size for i in entries) > MAX_ZIP_TOTAL:
                raise WorkAgentStoreError("Office document exceeds safe archive limits")
            if any(i.flag_bits & 1 for i in entries):
                raise WorkAgentStoreError("Encrypted Office documents are unsupported")
            names = {i.filename for i in entries}
            if expected and expected not in names:
                raise WorkAgentStoreError("Invalid Office document")
            output = {}
            for item in entries:
                if item.is_dir():
                    continue
                if item.file_size > MAX_EXTRACTED_TEXT * 16:
                    raise WorkAgentStoreError("Office document entry exceeds safe limits")
                # Reading through ZipFile verifies CRC and enforces declared sizes.
                output[item.filename] = archive.read(item)
            return output
        except WorkAgentStoreError:
            raise
        except (zipfile.BadZipFile, RuntimeError, OSError, KeyError) as exc:
            raise WorkAgentStoreError("Invalid or encrypted Office document") from exc

    @classmethod
    def _extract_text(cls, raw: bytes, suffix: str) -> str:
        if suffix in TEXT_EXTENSIONS:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("utf-16")
                except UnicodeDecodeError as exc:
                    raise WorkAgentStoreError("Text file must use UTF-8 or UTF-16 encoding") from exc
        elif suffix == ".pdf":
            try:
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(raw), strict=True)
                if reader.is_encrypted:
                    raise WorkAgentStoreError("Encrypted PDFs are unsupported")
                pages = []
                for page in reader.pages[:50]:
                    pages.append(page.extract_text() or "")
                    if sum(map(len, pages)) > MAX_EXTRACTED_TEXT:
                        break
                text = "\n".join(pages)
                if not text.strip():
                    raise WorkAgentStoreError("This PDF has no extractable text; scanned PDFs are not supported.")
            except WorkAgentStoreError:
                raise
            except Exception as exc:
                raise WorkAgentStoreError("Invalid or unsupported PDF") from exc
        elif suffix in {".docx", ".xlsx", ".pptx"}:
            expected = {".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}[suffix]
            entries = cls._zip_read(raw, expected)
            xml_names = [n for n in entries if n.endswith(".xml") and (suffix != ".xlsx" or n.startswith(("xl/worksheets/", "xl/sharedStrings.xml"))) and (suffix != ".pptx" or n.startswith("ppt/slides/"))]
            chunks = []
            for filename in xml_names:
                try:
                    root = ElementTree.fromstring(entries[filename])
                except ElementTree.ParseError as exc:
                    raise WorkAgentStoreError("Malformed Office document XML") from exc
                chunks.extend((node.text or "") for node in root.iter() if node.tag.endswith("}t") or node.tag.endswith("}v"))
            text = "\n".join(chunks)
        else:
            raise WorkAgentStoreError("Unsupported attachment type")
        return text[:MAX_EXTRACTED_TEXT]

    def prepare_turn(self, sid: str, text: str, attachment_ids: list[str]) -> dict[str, Any]:
        directory = self._session_dir(sid)
        manifest = self._read_manifest(directory)
        if not isinstance(text, str) or not isinstance(attachment_ids, list):
            raise WorkAgentStoreError("Invalid turn input")
        if len(set(attachment_ids)) != len(attachment_ids) or len(attachment_ids) > MAX_FILES_PER_TURN:
            raise WorkAgentStoreError("Invalid attachment selection")
        by_id = {a["id"]: a for a in manifest.get("attachments", [])}
        if any(aid not in by_id for aid in attachment_ids):
            raise WorkAgentStoreError("Attachment does not belong to this conversation")
        recent_refs = []
        for message in reversed(manifest.get("messages", [])[-12:]):
            recent_refs.extend(reversed(message.get("attachment_ids", [])))
        # Current-turn selections take priority, followed by the most recently referenced materials.
        selected = []
        for aid in [*reversed(attachment_ids), *recent_refs]:
            if aid in by_id and aid not in selected:
                selected.append(aid)
            if len(selected) == 8:
                break
        payload = []
        prepared_chars = 0
        prepared_limit = 40_000
        image_count = 0
        for aid in selected:
            attachment = by_id[aid]
            path = (directory / "attachments" / attachment["_file"]).resolve()
            if path.parent != (directory / "attachments").resolve() or not path.is_file():
                raise WorkAgentStoreError("Stored attachment is unavailable")
            data = path.read_bytes()
            if attachment["kind"] == "image":
                if image_count >= 3:
                    continue
                image_count += 1
                mime = attachment.get("mime_type", attachment.get("mime"))
                payload.append({"id": aid, "name": attachment["name"], "mime": mime, "mime_type": mime, "kind": "image", "data": data})
            else:
                extracted = self._extract_text(data, attachment["_source_type"])
                remaining = max(0, prepared_limit - prepared_chars)
                bounded = extracted[:remaining]
                prepared_chars += len(bounded)
                mime = attachment.get("mime_type", attachment.get("mime"))
                payload.append({"id": aid, "name": attachment["name"], "mime": mime, "mime_type": mime, "kind": "text", "text": bounded})
        history = [{k: v for k, v in message.items() if k in {"id", "created_at", "role", "content", "attachment_ids", "sources", "trace", "artifact_ids"}} for message in manifest.get("messages", [])[-12:]]
        pending = {"id": _new_id(), "created_at": _now(), "role": "user", "content": text[:MAX_EXTRACTED_TEXT], "attachment_ids": selected}
        return {"messages": history + [pending], "attachments": payload}

    @_synchronized
    def commit_turn(self, sid: str, text: str, attachment_ids: list[str], result: dict[str, Any]) -> dict[str, Any]:
        directory = self._session_dir(sid)
        manifest = self._read_manifest(directory)
        prepared = self.prepare_turn(sid, text, attachment_ids)
        assistant_text = result.get("answer", result.get("text", result.get("content", "")))
        if not isinstance(assistant_text, str):
            assistant_text = str(assistant_text)
        now = _now()
        user_message = prepared["messages"][-1]
        user_message["created_at"] = now
        manifest.setdefault("messages", []).append(user_message)
        artifacts = result.get("artifacts", [])
        if isinstance(artifacts, dict):
            artifacts = [{"name": name, "content": value} for name, value in artifacts.items()]
        if not isinstance(artifacts, list) or len(artifacts) > MAX_ARTIFACTS:
            raise WorkAgentStoreError(f"A turn may create at most {MAX_ARTIFACTS} artifacts")
        artifact_ids = []
        created = []
        artifact_records = []
        try:
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    raise WorkAgentStoreError("Invalid artifact")
                name = self._safe_name(str(artifact.get("name", "artifact")))
                content = artifact.get("data", artifact.get("content", b""))
                if isinstance(content, str):
                    content = content.encode("utf-8")
                if not isinstance(content, bytes) or len(content) > MAX_ARTIFACT_SIZE:
                    raise WorkAgentStoreError("Artifact must be text and at most 100 KB")
                try:
                    content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise WorkAgentStoreError("Binary artifacts are unsupported") from exc
                mime = artifact.get("mime_type", artifact.get("mime", mimetypes.guess_type(name)[0] or "text/plain"))
                if not isinstance(mime, str) or mime not in {"text/plain", "text/markdown", "text/csv", "application/json"}:
                    raise WorkAgentStoreError("Artifact MIME type must be text")
                if not re.fullmatch(r"[\w][\w .-]{0,99}", name, re.UNICODE) or ".." in name:
                    raise WorkAgentStoreError("Invalid artifact name")
                if mime == "application/json":
                    try:
                        json.loads(content)
                    except json.JSONDecodeError as exc:
                        raise WorkAgentStoreError("Invalid JSON artifact") from exc
                aid = _new_id()
                suffix = Path(name).suffix
                target = directory / "artifacts" / f"{aid}{suffix}"
                target.write_bytes(content)
                created.append(target)
                artifact_records.append({"id": aid, "name": name, "mime_type": mime, "size": len(content), "_file": target.name})
                artifact_ids.append(aid)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        manifest.setdefault("artifacts", []).extend(artifact_records)
        assistant = {"id": _new_id(), "created_at": now, "role": "assistant", "content": assistant_text, "attachment_ids": [], "artifact_ids": artifact_ids}
        for key in ("sources", "trace"):
            if key in result:
                assistant[key] = result[key]
        manifest["messages"].append(assistant)
        if len(manifest["messages"]) == 2:
            selected_names = [next((a["name"] for a in manifest.get("attachments", []) if a["id"] == aid), "") for aid in user_message["attachment_ids"]]
            first = text.strip().splitlines()[0] if text.strip() else next((n for n in selected_names if n), "新任务")
            manifest["title"] = first[:80]
        manifest["updated_at"] = _now()
        self._write_manifest(directory, manifest)
        return self._public(manifest)

    def resolve_artifact(self, sid: str, artifact_id: str) -> Path:
        directory = self._session_dir(sid)
        manifest = self._read_manifest(directory)
        artifact = next((a for a in manifest.get("artifacts", []) if a.get("id") == artifact_id), None)
        if artifact is None:
            raise WorkAgentStoreError("Artifact not found")
        path = (directory / "artifacts" / artifact["_file"]).resolve()
        if path.parent != (directory / "artifacts").resolve() or not path.is_file():
            raise WorkAgentStoreError("Artifact is unavailable")
        return path

    @_synchronized
    def delete_session(self, sid: str) -> None:
        directory = self._session_dir(sid)
        shutil.rmtree(directory)
