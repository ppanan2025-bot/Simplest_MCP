from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unified import hub, ids, ingest, search, store


HUB_SCHEMA = """
CREATE TABLE units (uos_code TEXT PRIMARY KEY, title TEXT, created_at TEXT);
CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    uos_code TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    title TEXT NOT NULL,
    week INTEGER,
    source_filename TEXT NOT NULL,
    stored_path TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    rowid_n INTEGER UNIQUE,
    document_id TEXT NOT NULL,
    uos_code TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    page INTEGER,
    heading TEXT,
    content TEXT NOT NULL,
    takeaway TEXT NOT NULL,
    created_at TEXT
);
CREATE VIRTUAL TABLE chunks_fts USING fts5(content, takeaway, heading, tokenize = 'porter');
"""


class UnifiedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub_db = self.root / "knowledge.db"
        self.unified_db = self.root / "unified.db"
        self.images = self.root / "images"
        self.images.mkdir()
        self._build_hub()
        self.env = mock.patch.dict(
            "os.environ",
            {
                "KNOWLEDGE_HUB_DB": str(self.hub_db),
                "UNIFIED_DB_PATH": str(self.unified_db),
                "ONENOTE_IMAGE_DIR": str(self.images),
            },
            clear=False,
        )
        self.env.start()
        store.init_db().close()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _build_hub(self) -> None:
        conn = sqlite3.connect(self.hub_db)
        conn.executescript(HUB_SCHEMA)
        conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
            (
                "aa4d89d8a6ba4db49548fb72113ef5bc",
                "COMP2022",
                "lecture",
                "normalization",
                3,
                "bcnf.pdf",
                None,
                "2026-09-01",
            ),
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "c0ffee00c0ffee00c0ffee00c0ffee00",
                1,
                "aa4d89d8a6ba4db49548fb72113ef5bc",
                "COMP2022",
                "lecture",
                4,
                "BCNF",
                "BCNF is a stricter form of 3NF used in database normalization.",
                "BCNF is stricter than 3NF",
                "2026-09-01",
            ),
        )
        conn.execute(
            "INSERT INTO chunks_fts(rowid, content, takeaway, heading) VALUES (?,?,?,?)",
            (1, "BCNF is a stricter form of 3NF used in database normalization.", "BCNF is stricter than 3NF", "BCNF"),
        )
        conn.commit()
        conn.close()

    def test_rejects_path_ids(self) -> None:
        self.assertEqual(ids.validate_id("../etc/passwd", "source")["code"], "INVALID_ID")
        self.assertEqual(search.get_source("onenote:id; curl")["code"], "INVALID_ID")
        self.assertEqual(search.get_source_image("/etc/shadow")["code"], "INVALID_ID")

    def test_pdf_search_uses_hub_fts(self) -> None:
        result = search.unified_search("BCNF", source_type="pdf")
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["source_type"], "pdf")
        self.assertIn("BCNF", result["matches"][0]["text"])
        self.assertTrue(result["matches"][0]["chunk_id"].startswith("pdf:"))
        self.assertFalse(result["vector_search"])

    def test_onenote_search_and_image(self) -> None:
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
        image_path = self.images / "pda.png"
        image_path.write_bytes(png)
        page = {
            "ok": True,
            "id": "0-ABC!1",
            "title": "PDA",
            "created": "2026-09-01",
            "text": "A PDA is a finite automaton with a stack.",
            "image_count": 1,
            "image_files": [str(image_path)],
            "_image_blobs": [{"data": png, "format": "png"}],
        }
        with mock.patch.object(ingest.onenote, "_validate_id", return_value="0-ABC!1"):
            with mock.patch.object(ingest.onenote, "read_onenote_page", return_value=dict(page)):
                stored = ingest.ingest_onenote_page(
                    {"id": "0-ABC!1", "title": "PDA", "modified": "2026-09-02"},
                    "ISCF2003",
                    "automata",
                )
        self.assertTrue(stored["ok"])
        result = search.unified_search("PDA", source_type="onenote")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["count"], 1)
        self.assertEqual(result["matches"][0]["source_type"], "onenote")
        source = search.get_source(stored["source_id"])
        self.assertEqual(source["category"], "ISCF2003")
        image_id = source["image_ids"][0]
        image = search.get_source_image(image_id)
        self.assertTrue(image["ok"])
        self.assertEqual(image["data"], png)
        self.assertNotIn("access_token", str(image))

        skipped = ingest.ingest_onenote_page(
            {"id": "0-ABC!1", "title": "PDA", "modified": "2026-09-02"},
            "ISCF2003",
            "automata",
        )
        self.assertTrue(skipped.get("skipped"))

    def test_unified_search_merges_sources(self) -> None:
        conn = store.init_db()
        conn.execute(
            "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "onenote:0-XYZ!1",
                "onenote",
                "normalization notes",
                "ISYS2120",
                "sql",
                "0-XYZ!1",
                "2026-09-01",
                "2026-09-03",
                "abc",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                "onenote:0-XYZ!1:c0",
                1,
                "onenote:0-XYZ!1",
                "Handwritten BCNF example from tutorial",
                "sql",
                "normalization notes",
                None,
                0,
                "handwriting",
                None,
            ),
        )
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text, title, section) VALUES (?,?,?,?)",
            (1, "Handwritten BCNF example from tutorial", "normalization notes", "sql"),
        )
        conn.commit()
        conn.close()
        result = search.unified_search("BCNF")
        kinds = {item["source_type"] for item in result["matches"]}
        self.assertIn("pdf", kinds)
        self.assertIn("onenote", kinds)

    def test_recent_sources(self) -> None:
        result = search.get_recent_sources(5)
        self.assertTrue(result["ok"])
        self.assertTrue(any(item["source_type"] == "pdf" for item in result["sources"]))


if __name__ == "__main__":
    unittest.main()
