"""Portable local settings; credentials encrypted for the current Windows user."""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import threading
import uuid
from pathlib import Path
from ctypes import wintypes

DEFAULTS = {
    'base_url': 'https://openrouter.ai/api', 'model_name': 'typesafe/jev-1.13',
    'reply_model': '', 'reply_base_url': '', 'relationship': '朋友', 'style': '', 'reply_to': '',
    'auto_analyze': True, 'context_limit': 30, 'save_history': False,
    'always_on_top': False, 'source': 'ocr', 'weflow_url': 'http://127.0.0.1:5031',
    'debounce_ms': 1200,
}
SECRETS = ('api_key', 'reply_api_key', 'weflow_token')


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(raw: bytes, decrypt: bool = False) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('密钥加密需要 Windows 用户环境。')
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    source, target = Blob(len(raw), buffer), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), 'Zhixian credentials', None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise RuntimeError('无法访问此 Windows 用户的加密密钥，请在设置中重新填写。')
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel.LocalFree(target.pbData)


def _atomic(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (FileNotFoundError, ValueError, OSError):
        return default


def normalize_title(value):
    return re.sub(r'\s*[（(]\d+[）)]\s*$', '', str(value)).strip().casefold()


class Store:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.error = ''
        stored = _load(self.directory / 'config.json', {})
        self.config = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
        self.secrets = {k: '' for k in SECRETS}
        encrypted = _load(self.directory / 'credentials.json', {})
        if encrypted.get('protected'):
            try:
                decoded = json.loads(_crypt(base64.b64decode(encrypted['protected']), True))
                self.secrets.update({k: str(decoded.get(k, '')) for k in SECRETS})
            except Exception:
                self.error = '已保存的密钥无法解密，请在当前 Windows 用户下重新填写。'
        kb = _load(self.directory / 'knowledge.json', {})
        self.notes = kb.get('notes', []) if isinstance(kb, dict) else []
        self.contacts = kb.get('contacts', []) if isinstance(kb, dict) else []

    def full_config(self):
        with self.lock:
            return {**self.config, **self.secrets}

    def public_config(self):
        with self.lock:
            return {**self.config, 'has_api_key': bool(self.secrets['api_key']),
                    'has_reply_api_key': bool(self.secrets['reply_api_key']),
                    'weflow_has_token': bool(self.secrets['weflow_token'])}

    def merged(self, changes):
        result = self.full_config()
        for key, value in changes.items():
            if key in DEFAULTS:
                result[key] = value
            elif key in SECRETS and str(value).strip():
                result[key] = str(value).strip()
        return result

    def save_config(self, changes):
        with self.lock:
            config = self.merged(changes)
            for key in ('base_url', 'model_name', 'reply_model', 'reply_base_url', 'style', 'relationship', 'weflow_url', 'reply_to'):
                config[key] = str(config.get(key, '')).strip()[:8000 if key == 'style' else 2000]
            if not config['base_url'] or not config['model_name']:
                raise ValueError('请填写 Base URL 和模型名称。')
            from core.client import validate_url
            from .weflow import validate_url as validate_weflow
            config['base_url'] = validate_url(config['base_url'])
            if config['reply_base_url']:
                config['reply_base_url'] = validate_url(config['reply_base_url'])
            config['weflow_url'] = validate_weflow(config['weflow_url'])
            config['context_limit'] = min(200, max(3, int(config['context_limit'])))
            config['debounce_ms'] = min(10000, max(400, int(config['debounce_ms'])))
            for key in ('auto_analyze', 'save_history', 'always_on_top'):
                config[key] = bool(config[key])
            if config['source'] not in ('ocr', 'weflow', 'auto'):
                raise ValueError('不支持的消息来源。')
            for key in SECRETS:
                if changes.get('clear_' + key):
                    config[key] = ''
            new_secrets = {k: config[k] for k in SECRETS}
            # Encrypt before persisting settings so failure never falls back to plaintext.
            protected = base64.b64encode(_crypt(json.dumps(new_secrets).encode())).decode()
            self.config = {k: config[k] for k in DEFAULTS}
            self.secrets = new_secrets
            _atomic(self.directory / 'credentials.json', {'version': 1, 'protected': protected})
            _atomic(self.directory / 'config.json', self.config)
            if not self.config['save_history']:
                (self.directory / 'history.json').unlink(missing_ok=True)
            return self.public_config()

    def _save_kb(self):
        _atomic(self.directory / 'knowledge.json', {'notes': self.notes, 'contacts': self.contacts})

    def save_note(self, note):
        with self.lock:
            content = str(note.get('content', '')).strip()[:16000]
            title = str(note.get('title', '')).strip()[:200]
            if not title or not content:
                raise ValueError('笔记标题和内容不能为空。')
            tags = note.get('tags', [])
            if isinstance(tags, str):
                tags = re.split(r'[,，\n ]+', tags)
            item = {'id': str(note.get('id') or uuid.uuid4().hex), 'title': title, 'content': content,
                    'tags': [str(t).strip()[:100] for t in tags if str(t).strip()][:30],
                    'always': bool(note.get('always', False))}
            self.notes = [n for n in self.notes if n['id'] != item['id']] + [item]
            self._save_kb()
            return item

    def save_contact(self, contact):
        with self.lock:
            name = str(contact.get('name', '')).strip()[:200]
            if not name:
                raise ValueError('请填写联系人名称。')
            aliases = contact.get('aliases', [])
            if isinstance(aliases, str):
                aliases = re.split(r'[,，\n]+', aliases)
            item = {'id': str(contact.get('id') or uuid.uuid4().hex), 'name': name,
                    'aliases': [str(a).strip()[:200] for a in aliases if str(a).strip()][:30],
                    'relationship': str(contact.get('relationship', '')).strip()[:500],
                    'notes': str(contact.get('notes', '')).strip()[:8000]}
            self.contacts = [c for c in self.contacts if c['id'] != item['id']] + [item]
            self._save_kb()
            return item

    def delete(self, kind, ident):
        with self.lock:
            values = self.notes if kind == 'notes' else self.contacts
            values[:] = [n for n in values if n['id'] != ident]
            self._save_kb()

    def background(self, title, messages):
        with self.lock:
            haystack = (title + ' ' + ' '.join(str(m.get('text', '')) for m in messages[-6:])).casefold()
            contact = next((c for c in self.contacts if normalize_title(title) in
                            [normalize_title(x) for x in [c['name'], *c['aliases']]]), None)
            selected = [n for n in self.notes if n['always']]
            selected += [n for n in self.notes if not n['always'] and any(
                t.casefold() in haystack for t in [n['title'], *n['tags']] if t)][:5]
            parts = []
            if contact:
                parts.append(f"联系人：{contact['name']}\n关系：{contact['relationship']}\n备注：{contact['notes']}")
            parts.extend(f"笔记：{n['title']}\n{n['content']}" for n in selected)
            return '\n\n'.join(parts)[:24000], contact

    def load_history(self):
        return _load(self.directory / 'history.json', []) if self.config['save_history'] else []

    def save_history(self, sessions):
        with self.lock:
            if self.config['save_history']:
                _atomic(self.directory / 'history.json', sessions[-50:])

    def clear_history(self):
        with self.lock:
            (self.directory / 'history.json').unlink(missing_ok=True)
