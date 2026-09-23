"""Read-only access to the existing Knowledge Hub SQLite (PDFs and lecture notes)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from . import ids

HUB_DB = Path(os.environ.get("KNOWLEDGE_HUB_DB", "/opt/knowledge-hub/data/knowledge.db"))


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def hub_db_path() -> Path:
    return Path(os.environ.get("KNOWLEDGE_HUB_DB", str(HUB_DB)))


def connect_hub() -> sqlite3.Connection | dict:
    path = hub_db_path()
    if not path.is_file():
        return _error("HUB_UNAVAILABLE", "Knowledge hub database is not available")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _error("HUB_UNAVAILABLE", f"Could not open knowledge hub database: {exc}")
    conn.row_factory = sqlite3.Row
    return conn


def fts_query(query: str) -> str:
    terms = [term.strip() for term in query.replace('"', " ").split() if term.strip()]
    if not terms:
        return '""'
    return " AND ".join(f'"{term}"' for term in terms)


def relevance(raw: object) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.35
    return 1.0 / (1.0 + max(value, 0.0))


def search_pdfs(query: str, limit: int) -> list[dict]:
    conn = connect_hub()
    if isinstance(conn, dict):
        return []
    try:
        rows = conn.execute(
            """
            SELECT
                c.id AS chunk_id,
                c.document_id,
                d.title,
                d.uos_code,
                d.doc_type,
                c.page,
                c.heading,
                c.content,
                bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.rowid_n = chunks_fts.rowid
            JOIN documents d ON d.id = c.document_id
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (fts_query(query), limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    results = []
    for row in rows:
        excerpt = (row["content"] or "")[:280]
        results.append(
            {
                "source_id": ids.pdf_source_id(row["document_id"]),
                "source_type": "pdf",
                "title": row["title"],
                "page": row["page"],
                "section": row["heading"] or row["uos_code"],
                "chunk_id": ids.pdf_chunk_id(row["chunk_id"]),
                "text": excerpt,
                "image_id": None,
                "relevance": relevance(row["score"]),
                "category": row["uos_code"],
            }
        )
    return results


def get_pdf_source(document_id: str) -> dict:
    conn = connect_hub()
    if isinstance(conn, dict):
        return conn
    try:
        row = conn.execute(
            """
            SELECT d.id, d.uos_code, d.doc_type, d.title, d.week, d.source_filename,
                   d.created_at, COUNT(c.id) AS chunk_count
            FROM documents d
            LEFT JOIN chunks c ON c.document_id = d.id
            WHERE d.id = ?
            GROUP BY d.id
            """,
            (document_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return _error("NOT_FOUND", "PDF source not found")
    return {
        "ok": True,
        "source_id": ids.pdf_source_id(row["id"]),
        "source_type": "pdf",
        "title": row["title"],
        "course": row["uos_code"],
        "category": row["uos_code"],
        "section": row["doc_type"],
        "source_ref": row["source_filename"],
        "created_at": row["created_at"],
        "updated_at": row["created_at"],
        "chunk_count": row["chunk_count"],
        "week": row["week"],
    }


def get_pdf_chunk(chunk_id: str) -> dict:
    conn = connect_hub()
    if isinstance(conn, dict):
        return conn
    try:
        row = conn.execute(
            """
            SELECT c.id, c.document_id, c.page, c.heading, c.content, c.takeaway,
                   d.title, d.uos_code, d.doc_type
            FROM chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return _error("NOT_FOUND", "PDF chunk not found")
    return {
        "ok": True,
        "chunk_id": ids.pdf_chunk_id(row["id"]),
        "source_id": ids.pdf_source_id(row["document_id"]),
        "source_type": "pdf",
        "title": row["title"],
        "page": row["page"],
        "section": row["heading"] or row["doc_type"],
        "text": row["content"],
        "image_id": None,
        "category": row["uos_code"],
    }


def surrounding_pdf(chunk_id: str, window: int = 2) -> dict:
    center = get_pdf_chunk(chunk_id)
    if not center.get("ok"):
        return center
    document_id = ids.parse_prefixed(center["source_id"])[1]
    conn = connect_hub()
    if isinstance(conn, dict):
        return conn
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.page, c.heading, c.content
            FROM chunks c
            WHERE c.document_id = ?
            ORDER BY c.page, c.id
            """,
            (document_id,),
        ).fetchall()
    finally:
        conn.close()
    index = next((i for i, row in enumerate(rows) if row["id"] == chunk_id), None)
    if index is None:
        return center
    start = max(0, index - window)
    end = min(len(rows), index + window + 1)
    neighbors = []
    for row in rows[start:end]:
        neighbors.append(
            {
                "chunk_id": ids.pdf_chunk_id(row["id"]),
                "page": row["page"],
                "section": row["heading"],
                "text": (row["content"] or "")[:400],
                "current": row["id"] == chunk_id,
            }
        )
    return {"ok": True, "source_id": center["source_id"], "chunks": neighbors}


def recent_pdf_sources(limit: int) -> list[dict]:
    conn = connect_hub()
    if isinstance(conn, dict):
        return []
    try:
        rows = conn.execute(
            """
            SELECT id, title, uos_code, doc_type, created_at
            FROM documents
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "source_id": ids.pdf_source_id(row["id"]),
            "source_type": "pdf",
            "title": row["title"],
            "category": row["uos_code"],
            "section": row["doc_type"],
            "updated_at": row["created_at"],
        }
        for row in rows
    ]
