"""Synthetic media only. Network is mocked or served by a loopback HTTP server."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import wave

from PIL import Image, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from core import media
from core.client import ProviderError

CONFIG = {"base_url": "https://openrouter.ai/api", "model_name": "typesafe/jev-1.13",
          "api_key": "test-secret-value", "media_allow_cloud": True}


def image_bytes():
    out = io.BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text("comment", "synthetic-private-metadata")
    Image.new("RGB", (24, 16), "orange").save(out, format="PNG", pnginfo=info)
    return out.getvalue()


def audio_bytes(seconds=0.05):
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\x00\x00" * int(seconds * 16000))
    return out.getvalue()


class Response:
    def __init__(self, value):
        self.value = value if isinstance(value, bytes) else json.dumps(value).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self, n=-1):
        return self.value if n < 0 else self.value[:n]


class Transport:
    def __init__(self, answer=None, inputs=None, outputs=None):
        self.requests = []
        self.answer = answer or {"choices": [{"message": {"content": "一张橙色的合成测试图片"}}]}
        self.inputs = inputs or ["image", "audio", "text"]
        self.outputs = outputs or ["text", "transcription"]
    def open(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append((request, body, timeout))
        if body is None:
            return Response({"data": {"architecture": {"input_modalities": self.inputs, "output_modalities": self.outputs}, "endpoints": [{"name": "synthetic"}]}})
        if callable(self.answer):
            return Response(self.answer(request, body))
        return Response(self.answer)


class MediaTests(unittest.TestCase):
    def setUp(self):
        media._CATALOG_CACHE.clear()

    def transport(self, **kwargs):
        transport = Transport(**kwargs)
        patcher = patch("core.client.urllib.request.build_opener", return_value=transport)
        patcher.start()
        self.addCleanup(patcher.stop)
        return transport

    def test_image_actual_protocol_and_metadata_removed(self):
        transport = self.transport()
        result = media.analyze_image(image_bytes(), "image/png", CONFIG)
        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["confidence"])
        self.assertEqual(result["model"], media.DEFAULT_VISION_MODEL)
        self.assertEqual(len(transport.requests), 2)
        metadata_request = transport.requests[0][0]
        self.assertIsNone(metadata_request.get_header("Authorization"))
        request, body, _ = transport.requests[1]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + CONFIG["api_key"])
        content = body["messages"][1]["content"]
        self.assertEqual([p["type"] for p in content], ["text", "image_url"])
        uploaded = base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1])
        with Image.open(io.BytesIO(uploaded)) as parsed:
            self.assertNotIn("comment", parsed.info)
            self.assertEqual(parsed.size, (24, 16))
        self.assertNotIn(CONFIG["api_key"], json.dumps(result, ensure_ascii=False))

    def test_cloud_disabled_never_queries_or_uploads(self):
        transport = self.transport()
        result = media.analyze_image(image_bytes(), "image/png", dict(CONFIG, media_allow_cloud=False))
        self.assertEqual(result["code"], "cloud_disabled")
        self.assertEqual(transport.requests, [])

    def test_bad_image_format_dimensions_source_and_bytes_rejected(self):
        transport = self.transport()
        for data, mime, source in ((b"not-image", "image/png", "user_selected"), (image_bytes(), "image/jpeg", "user_selected"), (image_bytes(), "image/png", "https://example.invalid/private.png"), ("private.png", "image/png", "user_selected")):
            with self.subTest(mime=mime, source=source), self.assertRaises(ProviderError):
                media.analyze_image(data, mime, CONFIG, source=source)
        with patch.object(media, "MAX_IMAGE_BYTES", 2), self.assertRaises(ProviderError):
            media.analyze_image(image_bytes(), "image/png", CONFIG)
        with patch.object(media, "MAX_IMAGE_PIXELS", 10), self.assertRaises(ProviderError):
            media.analyze_image(image_bytes(), "image/png", CONFIG)
        self.assertEqual(transport.requests, [])

    def test_model_without_image_capability_returns_unsupported_before_upload(self):
        transport = self.transport(inputs=["text"], outputs=["text"])
        result = media.analyze_image(image_bytes(), "image/png", dict(CONFIG, vision_model="vendor/text-only-model"))
        self.assertEqual(result["code"], "model_capability_missing")
        self.assertFalse(result["success"])
        self.assertEqual(len(transport.requests), 1)
        self.assertIsNone(transport.requests[0][1])

    def test_typesafe_and_jev_are_not_misrepresented_as_vision(self):
        transport = self.transport()
        configs = [dict(CONFIG, base_url="https://api.typesafe.ai/v1"), dict(CONFIG, vision_model="typesafe/jev-1.13")]
        for config in configs:
            result = media.analyze_image(image_bytes(), "image/png", config)
            self.assertEqual(result["status"], "unsupported")
            self.assertEqual(result["text"], "")
        self.assertEqual(transport.requests, [])

    def test_cross_origin_key_requires_explicit_media_credential(self):
        config = dict(CONFIG, vision_base_url="https://vision.example.invalid/v1", vision_model="vision-custom")
        with self.assertRaises(ProviderError):
            media.analyze_image(image_bytes(), "image/png", config)
        transport = self.transport()
        result = media.analyze_image(image_bytes(), "image/png", dict(config, vision_api_key="separate-test-key"))
        self.assertTrue(result["success"])
        self.assertEqual(transport.requests[0][0].get_header("Authorization"), "Bearer separate-test-key")
        self.assertEqual(transport.requests[0][1]["model"], "vision-custom")

    def test_service_unsupported_and_http_error_never_reflect_key(self):
        def rejected(request, body):
            raise urllib.error.HTTPError(request.full_url, 415, CONFIG["api_key"], {}, io.BytesIO(CONFIG["api_key"].encode()))
        self.transport(answer=rejected)
        result = media.analyze_image(image_bytes(), "image/png", CONFIG)
        self.assertEqual(result["code"], "media_protocol_rejected")
        self.assertNotIn(CONFIG["api_key"], json.dumps(result))

    def test_timeout_retries_are_bounded_and_error_is_redacted(self):
        def timeout(request, body):
            raise TimeoutError(CONFIG["api_key"])
        transport = self.transport(answer=timeout)
        with patch("core.client.time.sleep"), self.assertRaises(ProviderError) as error:
            media.analyze_image(image_bytes(), "image/png", dict(CONFIG, media_timeout=500))
        self.assertEqual(len(transport.requests), 3)  # one metadata GET, two POST attempts
        self.assertEqual(transport.requests[0][2], 12)
        self.assertEqual(transport.requests[-1][2], 60)
        self.assertNotIn(CONFIG["api_key"], str(error.exception))

    def test_failed_capability_lookup_does_not_upload_media_or_echo_remote_body(self):
        class FailedCatalog:
            def __init__(self):
                self.requests = []
            def open(self, request, timeout):
                self.requests.append(request)
                raise urllib.error.HTTPError(request.full_url, 403, CONFIG["api_key"], {}, io.BytesIO(CONFIG["api_key"].encode()))
        failed = FailedCatalog()
        with patch("core.client.urllib.request.build_opener", return_value=failed), self.assertRaises(ProviderError) as error:
            media.analyze_image(image_bytes(), "image/png", CONFIG)
        self.assertEqual(len(failed.requests), 1)
        self.assertIsNone(failed.requests[0].data)
        self.assertIsNone(failed.requests[0].get_header("Authorization"))
        self.assertNotIn(CONFIG["api_key"], str(error.exception))

    def test_jpeg_orientation_is_applied_before_metadata_removed(self):
        out = io.BytesIO()
        image = Image.new("RGB", (10, 20), "blue")
        exif = Image.Exif()
        exif[274] = 6
        image.save(out, format="JPEG", exif=exif)
        transport = self.transport()
        result = media.analyze_image(out.getvalue(), "image/jpeg", CONFIG)
        self.assertEqual((result["media"]["width"], result["media"]["height"]), (20, 10))
        upload = transport.requests[-1][1]["messages"][1]["content"][1]["image_url"]["url"]
        with Image.open(io.BytesIO(base64.b64decode(upload.split(",", 1)[1]))) as decoded:
            self.assertEqual(dict(decoded.getexif()), {})

    def test_invalid_media_response_does_not_invent_description(self):
        self.transport(answer={"choices": []})
        with self.assertRaises(ProviderError):
            media.analyze_image(image_bytes(), "image/png", CONFIG)

    def test_stt_dedicated_json_endpoint_and_payload(self):
        transport = self.transport(answer={"text": "合成转写结果", "usage": {"seconds": 0.05}, "confidence": None})
        original = audio_bytes()
        result = media.transcribe_audio(original, "audio/wav", CONFIG)
        self.assertEqual(result["kind"], "audio")
        self.assertEqual(result["text"], "合成转写结果")
        self.assertIsNone(result["confidence"])
        request, body, _ = transport.requests[-1]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/audio/transcriptions")
        self.assertNotIn("messages", body)
        self.assertEqual(body["input_audio"]["format"], "wav")
        self.assertEqual(base64.b64decode(body["input_audio"]["data"]), original)
        self.assertEqual(body["language"], "zh")

    def test_audio_empty_transcript_remains_empty(self):
        self.transport(answer={"text": "", "confidence": 3})
        result = media.transcribe_audio(audio_bytes(), "audio/wav", CONFIG)
        self.assertTrue(result["success"])
        self.assertEqual(result["text"], "")
        self.assertIsNone(result["confidence"])
        self.assertIn("未识别到", result["warning"])

    def test_audio_invalid_wav_and_length_rejected(self):
        transport = self.transport()
        for data in (b"RIFF-not-audio", audio_bytes()[:-10]):
            with self.assertRaises(ProviderError):
                media.transcribe_audio(data, "audio/wav", CONFIG)
        with patch.object(media, "MAX_AUDIO_SECONDS", 0.001), self.assertRaises(ProviderError):
            media.transcribe_audio(audio_bytes(), "audio/wav", CONFIG)
        with patch.object(media, "MAX_AUDIO_BYTES", 10), self.assertRaises(ProviderError):
            media.transcribe_audio(audio_bytes(), "audio/wav", CONFIG)
        self.assertEqual(transport.requests, [])

    def test_truncated_compressed_audio_headers_are_rejected(self):
        cases = ((b"\xff\xfb\x90\x00", "audio/mpeg"), (b"\xff\xf1\x50\x80\xff\xff\x00\x00", "audio/aac"), (b"OggS" + b"\x00" * 23 + b"OpusHead", "audio/ogg"))
        for data, mime in cases:
            with self.subTest(mime=mime), self.assertRaises(ProviderError):
                media.transcribe_audio(data, mime, CONFIG)

    def test_audio_chat_or_tts_model_is_not_assumed_to_be_stt(self):
        transport = self.transport(inputs=["audio"], outputs=["text", "audio"])
        result = media.transcribe_audio(audio_bytes(), "audio/wav", dict(CONFIG, stt_model="vendor/audio-chat-model"))
        self.assertEqual(result["code"], "model_capability_missing")
        self.assertEqual(len(transport.requests), 1)

    def test_silk_and_tts_endpoint_are_explicitly_unsupported(self):
        result = media.transcribe_audio(b"#!SILK_V3 synthetic", "audio/silk", CONFIG)
        self.assertEqual(result["code"], "audio_format")
        config = dict(CONFIG, stt_model="tts-model", stt_base_url="https://speech.example.invalid/v1/audio/speech")
        result = media.transcribe_audio(audio_bytes(), "audio/wav", config)
        self.assertEqual(result["code"], "wrong_media_endpoint")
        native_openai = dict(CONFIG, stt_model="whisper-1", stt_base_url="https://api.openai.com/v1")
        result = media.transcribe_audio(audio_bytes(), "audio/wav", native_openai)
        self.assertEqual(result["code"], "transcription_protocol")

    def test_plain_chat_completion_is_not_a_transcription(self):
        self.transport()
        with self.assertRaises(ProviderError) as error:
            media.transcribe_audio(audio_bytes(), "audio/wav", CONFIG)
        self.assertIn("text", str(error.exception))

    def test_missing_local_engine_does_not_fallback_to_paid_network(self):
        transport = self.transport()
        result = media.transcribe_audio(audio_bytes(), "audio/wav", dict(CONFIG, stt_backend="local"))
        self.assertEqual(result["code"], "local_stt_unavailable")
        self.assertEqual(transport.requests, [])

    def test_auto_prefers_complete_local_model_and_never_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for name in ("model.bin", "config.json", "tokenizer.json"):
                (root / name).write_bytes(b"synthetic test fixture")
            model = Mock()
            model.transcribe.return_value = (iter([SimpleNamespace(text="本地合成转写")]), SimpleNamespace(language="zh"))
            module = SimpleNamespace(WhisperModel=Mock(return_value=model))
            with patch.object(media.importlib.util, "find_spec", return_value=object()), patch.object(media, "post_json", side_effect=AssertionError("Must stay offline")), patch.object(media.importlib, "import_module", return_value=module):
                result = media.transcribe_audio(audio_bytes(), "audio/wav", dict(CONFIG, stt_local_model=str(root), media_allow_cloud=False))
            self.assertEqual(result["provider"], "local")
            self.assertEqual(result["text"], "本地合成转写")
            self.assertTrue(module.WhisperModel.call_args.kwargs["local_files_only"])
            self.assertIsInstance(model.transcribe.call_args.args[0], io.BytesIO)
            self.assertEqual(model.transcribe.call_args.kwargs["task"], "transcribe")

    def test_incomplete_local_model_never_invokes_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "model.bin").write_bytes(b"synthetic")
            with patch("core.media.importlib.import_module", side_effect=AssertionError("Missing tokenizer must not trigger a download")):
                result = media.transcribe_audio(audio_bytes(), "audio/wav", dict(CONFIG, stt_backend="local", stt_local_model=str(root)))
            self.assertEqual(result["code"], "local_stt_unavailable")

    def test_real_loopback_transcription_no_inherited_external_key(self):
        seen = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers.get("Authorization"), body))
                payload = json.dumps({"text": "仅本机合成响应"}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = dict(CONFIG, stt_base_url=f"http://127.0.0.1:{server.server_port}/v1", stt_model="local-gateway-stt", media_allow_cloud=False)
            result = media.transcribe_audio(audio_bytes(), "audio/wav", config)
            self.assertEqual(result["text"], "仅本机合成响应")
            self.assertEqual(seen[0][0], "/v1/audio/transcriptions")
            self.assertIsNone(seen[0][1])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
