import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image

from src.desk.work_agent_store import WorkAgentStore, WorkAgentStoreError


class WorkAgentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.store = WorkAgentStore(self.base / "data" / "work-agent")
        self.session = self.store.create_session()
        self.sid = self.session["id"]

    def tearDown(self):
        self.temp.cleanup()

    def attach(self, name, data):
        path = self.base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.store.attach_files(self.sid, [str(path)])[0]

    def test_text_selection_and_persistence(self):
        item = self.attach("hello.txt", b"hello world")
        turn = self.store.prepare_turn(self.sid, "Question", [item["id"]])
        self.assertEqual(turn["messages"][-1]["content"], "Question")
        self.assertEqual(turn["attachments"][0]["text"], "hello world")
        result = self.store.commit_turn(self.sid, "Question", [item["id"]], {"answer": "Answer", "sources": ["s"], "trace": {"x": 1}, "artifacts": [{"name": "out.txt", "content": "saved", "mime_type": "text/plain"}]})
        self.assertEqual([m["role"] for m in result["messages"]], ["user", "assistant"])
        self.assertEqual(result["messages"][1]["sources"], ["s"])
        self.assertTrue(result["messages"][1]["artifact_ids"])
        self.assertIn("mime_type", result["artifacts"][0])
        self.assertNotIn("_file", json.dumps(self.store.get_session(self.sid)))
        self.assertEqual(self.store.resolve_artifact(self.sid, result["artifacts"][0]["id"]).read_text(), "saved")
        restored = WorkAgentStore(self.store.root).get_session(self.sid)
        self.assertEqual(restored["messages"][1]["content"], "Answer")
        self.assertEqual(self.store.list_sessions()[0]["message_count"], 2)

    def test_prepare_turn_includes_bounded_history_and_separate_attachments(self):
        self.store.commit_turn(self.sid, "first", [], {"answer": "first answer"})
        item = self.attach("large.txt", b"z" * 120_000)
        turn = self.store.prepare_turn(self.sid, "follow up", [item["id"]])
        self.assertEqual([m["role"] for m in turn["messages"]], ["user", "assistant", "user"])
        self.assertEqual(turn["messages"][-1]["content"], "follow up")
        self.assertLessEqual(len(turn["attachments"][0]["text"]), 40_000)

    def test_followup_turn_reuses_recently_referenced_material(self):
        item = self.attach("reference.txt", b"remember this uploaded material")
        self.store.commit_turn(self.sid, "Read this", [item["id"]], {"answer": "I have read it"})
        second = self.store.prepare_turn(self.sid, "What did it say?", [])
        self.assertEqual(second["messages"][-1]["attachment_ids"], [item["id"]])
        self.assertEqual(second["attachments"][0]["id"], item["id"])
        self.assertIn("remember this uploaded material", second["attachments"][0]["text"])
        stored_user = self.store.get_session(self.sid)["messages"][0]
        self.assertEqual(second["messages"][-3]["id"], stored_user["id"])

    def test_image_reencoded_without_exif_and_bounded(self):
        image = Image.new("RGB", (2400, 600), "red")
        exif = Image.Exif()
        exif[271] = "private camera"
        data = io.BytesIO()
        image.save(data, "JPEG", exif=exif)
        item = self.attach("photo.jpg", data.getvalue())
        saved = self.store.root / self.sid / "attachments" / next((self.store.root / self.sid / "attachments").iterdir()).name
        with Image.open(saved) as reopened:
            self.assertLessEqual(max(reopened.size), 1600)
            self.assertNotIn(271, reopened.getexif())
        self.assertEqual(item["kind"], "image")
        self.assertLessEqual(item["size"], 2 * 1024 * 1024)

    def test_docx_and_zip_limits(self):
        buff = io.BytesIO()
        with zipfile.ZipFile(buff, "w") as z:
            z.writestr("word/document.xml", '<w:document xmlns:w="x"><w:t>DOCX TEXT</w:t></w:document>')
        item = self.attach("doc.docx", buff.getvalue())
        self.assertIn("DOCX TEXT", self.store.prepare_turn(self.sid, "", [item["id"]])["attachments"][0]["text"])
        with self.assertRaises(WorkAgentStoreError):
            WorkAgentStore._zip_read(b"PK not a zip", "word/document.xml")

    def test_pdf_extracts_or_reports_scanned_limitation(self):
        from pypdf import PdfWriter
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        buff = io.BytesIO()
        writer.write(buff)
        with self.assertRaisesRegex(WorkAgentStoreError, "scanned PDFs"):
            WorkAgentStore._extract_text(buff.getvalue(), ".pdf")

    def test_pdf_xlsx_and_pptx_extract_real_text(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf 72 720 Td (Synthetic PDF text) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        pdf = io.BytesIO()
        writer.write(pdf)
        self.assertIn('Synthetic PDF text', WorkAgentStore._extract_text(pdf.getvalue(), '.pdf'))

        for suffix, marker, expected, path, xml in (
            ('.xlsx', 'xl/workbook.xml', 'Sheet cell 42', 'xl/worksheets/sheet1.xml',
             '<x:sheet xmlns:x="urn:x"><x:t>Sheet cell 42</x:t></x:sheet>'),
            ('.pptx', 'ppt/presentation.xml', 'Slide title', 'ppt/slides/slide1.xml',
             '<a:slide xmlns:a="urn:a"><a:t>Slide title</a:t></a:slide>'),
        ):
            with self.subTest(suffix=suffix):
                buffer = io.BytesIO()
                with zipfile.ZipFile(buffer, 'w') as archive:
                    archive.writestr(marker, '<root/>')
                    archive.writestr(path, xml)
                self.assertIn(expected, WorkAgentStore._extract_text(buffer.getvalue(), suffix))

    def test_ids_names_and_artifacts_are_confined(self):
        with self.assertRaises(WorkAgentStoreError):
            self.store.get_session("../manifest.json")
        item = self.attach("..\\..\\private.txt", b"safe")
        self.assertNotIn("..", item["name"])
        with self.assertRaises(WorkAgentStoreError):
            self.store.prepare_turn(self.sid, "", ["../x"])
        output = self.store.commit_turn(self.sid, "", [], {"answer": "", "artifacts": [{"name": "../../bad.txt", "content": "ok", "mime_type": "text/plain"}]})
        self.assertNotIn("..", output["artifacts"][0]["name"])
        with self.assertRaises(WorkAgentStoreError):
            self.store.resolve_artifact(self.sid, "../bad")

    def test_artifact_limits_and_failed_attach_roll_back_files(self):
        with self.assertRaises(WorkAgentStoreError):
            self.store.commit_turn(self.sid, "", [], {"answer": "x", "artifacts": [{"name": "bad.bin", "content": b"\xff", "mime_type": "application/octet-stream"}]})
        bad = self.base / "unsupported.exe"
        bad.write_bytes(b"no")
        good = self.base / "ok.txt"
        good.write_text("ok")
        before = set((self.store.root / self.sid / "attachments").iterdir())
        with self.assertRaises(WorkAgentStoreError):
            self.store.attach_files(self.sid, [str(good), str(bad)])
        self.assertEqual(set((self.store.root / self.sid / "attachments").iterdir()), before)
        self.store.delete_session(self.sid)
        with self.assertRaises(WorkAgentStoreError):
            self.store.get_session(self.sid)


if __name__ == "__main__":
    unittest.main()
