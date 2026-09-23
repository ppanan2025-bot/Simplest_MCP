"""Unified keyword search over hub PDFs and indexed OneNote. Embeddings reserved."""

from __future__ import annotations

import os
from pathlib import Path

from . import hub, ids, store
from .ingest import refresh_unified_index

MIN_QUERY = 2
MAX_QUERY = 200
MAX_LIMIT = 30
WORKSPACE_ROOT = Path("/home/hermes/workspace").resolve()


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def _limit(value: object, default: int = 10) -> int | dict:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return _error("INVALID_LIMIT", "limit must be an integer")
    if limit < 1:
        return _error("INVALID_LIMIT", "limit must be at least 1")
    return min(limit, MAX_LIMIT)


def _image_dir() -> Path:
    override = os.environ.get("ONENOTE_IMAGE_DIR")
    if override:
        return Path(override).resolve()
    return WORKSPACE_ROOT


def _jailed_image_path(raw: str) -> Path | dict:
    text = (raw or "").strip()
    if not text:
        return _error("NOT_FOUND", "Image path is missing")
    if text.startswith("/workspace/"):
        text = str(WORKSPACE_ROOT / text[len("/workspace/") :])
    path = Path(text).resolve()
    roots = [_image_dir(), WORKSPACE_ROOT]
    if not any(path.is_relative_to(root) for root in roots if root.exists() or root == WORKSPACE_ROOT):
        return _error("PATH_DENIED", "Image is outside the allowed store")
    if not path.is_file():
        return _error("NOT_FOUND", "Image file is not available")
    return path


def search_onenote_index(query: str, limit: int) -> list[dict]:
    conn = store.init_db()
    try:
        rows = conn.execute(
            """
            SELECT
                c.chunk_id,
                c.source_id,
                s.title,
                s.category,
                c.section,
                c.text,
                c.image_id,
                c.kind,
                bm25(chunks_fts) AS score
            FROM chunks_fts
            JOIN chunks c ON c.rowid_n = chunks_fts.rowid
            JOIN sources s ON s.source_id = c.source_id
            WHERE chunks_fts MATCH ?
            ORDER BY score
            LIMIT ?
            """,
            (hub.fts_query(query), limit),
        ).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()

    results = []
    for row in rows:
        results.append(
            {
                "source_id": row["source_id"],
                "source_type": "onenote",
                "title": row["title"],
                "page": None,
                "section": row["section"] or row["category"],
                "chunk_id": row["chunk_id"],
                "text": (row["text"] or "")[:280],
                "image_id": row["image_id"],
                "relevance": hub.relevance(row["score"]),
                "category": row["category"],
                "kind": row["kind"],
            }
        )
    return results


def unified_search(query: str, limit: int = 10, source_type: str | None = None) -> dict:
    needle = (query or "").strip()
    if len(needle) < MIN_QUERY:
        return _error("INVALID_QUERY", "query must be at least 2 characters")
    if len(needle) > MAX_QUERY:
        return _error("INVALID_QUERY", "query is too long")
    checked = _limit(limit)
    if isinstance(checked, dict):
        return checked
    kind = (source_type or "").strip().lower() or None
    if kind and kind not in ids.SOURCE_TYPES:
        return _error("INVALID_SOURCE_TYPE", "source_type must be pdf or onenote")

    conn = store.init_db()
    onenote_count = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    conn.close()
    refreshed = None
    if onenote_count == 0 and kind != "pdf":
        refreshed = refresh_unified_index(max_pages=20)

    matches: list[dict] = []
    if kind in (None, "pdf"):
        matches.extend(hub.search_pdfs(needle, checked))
    if kind in (None, "onenote"):
        matches.extend(search_onenote_index(needle, checked))
    matches.sort(key=lambda item: item.get("relevance") or 0, reverse=True)
    result = {
        "ok": True,
        "query": needle,
        "source_type": kind,
        "count": min(len(matches), checked),
        "vector_search": False,
        "matches": matches[:checked],
    }
    if refreshed and not refreshed.get("ok"):
        result["onenote_index"] = refreshed
    return result


def get_source(source_id: str) -> dict:
    checked = ids.validate_id(source_id, "source")
    if isinstance(checked, dict):
        return checked
    prefix, rest = ids.parse_prefixed(checked)
    if prefix == "pdf":
        return hub.get_pdf_source(rest)
    conn = store.init_db()
    try:
        row = store.get_source_row(conn, checked)
        chunk_count = conn.execute(
            "SELECT COUNT(*) AS n FROM chunks WHERE source_id = ?", (checked,)
        ).fetchone()["n"]
        images = conn.execute(
            "SELECT image_id FROM images WHERE source_id = ?", (checked,)
        ).fetchall()
    finally:
        conn.close()
    if not row:
        return _error("NOT_FOUND", "OneNote source not found. Call refresh_unified_index.")
    return {
        "ok": True,
        "source_id": row["source_id"],
        "source_type": row["source_type"],
        "title": row["title"],
        "course": row["category"],
        "category": row["category"],
        "section": row["section"],
        "source_ref": row["source_ref"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "content_hash": row["content_hash"],
        "chunk_count": chunk_count,
        "image_ids": [item["image_id"] for item in images],
    }


def read_chunk(chunk_id: str) -> dict:
    checked = ids.validate_id(chunk_id, "chunk")
    if isinstance(checked, dict):
        return checked
    prefix, rest = ids.parse_prefixed(checked)
    if prefix == "pdf":
        return hub.get_pdf_chunk(rest)
    conn = store.init_db()
    try:
        row = store.get_chunk_row(conn, checked)
        source = store.get_source_row(conn, row["source_id"]) if row else None
    finally:
        conn.close()
    if not row:
        return _error("NOT_FOUND", "OneNote chunk not found")
    return {
        "ok": True,
        "chunk_id": row["chunk_id"],
        "source_id": row["source_id"],
        "source_type": "onenote",
        "title": source["title"] if source else row["page_title"],
        "page": None,
        "section": row["section"],
        "text": row["text"],
        "image_id": row["image_id"],
        "kind": row["kind"],
        "position": row["position"],
    }


def get_surrounding_context(chunk_id: str, window: int = 2) -> dict:
    checked = ids.validate_id(chunk_id, "chunk")
    if isinstance(checked, dict):
        return checked
    prefix, rest = ids.parse_prefixed(checked)
    if prefix == "pdf":
        return hub.surrounding_pdf(rest, window=window)
    conn = store.init_db()
    try:
        center = store.get_chunk_row(conn, checked)
        if not center:
            return _error("NOT_FOUND", "OneNote chunk not found")
        rows = conn.execute(
            """
            SELECT chunk_id, text, section, image_id, position
            FROM chunks
            WHERE source_id = ?
            ORDER BY position
            """,
            (center["source_id"],),
        ).fetchall()
    finally:
        conn.close()
    index = next((i for i, row in enumerate(rows) if row["chunk_id"] == checked), 0)
    start = max(0, index - window)
    end = min(len(rows), index + window + 1)
    return {
        "ok": True,
        "source_id": center["source_id"],
        "chunks": [
            {
                "chunk_id": row["chunk_id"],
                "section": row["section"],
                "text": (row["text"] or "")[:400],
                "image_id": row["image_id"],
                "current": row["chunk_id"] == checked,
            }
            for row in rows[start:end]
        ],
    }


def get_source_image(image_id: str) -> dict:
    checked = ids.validate_id(image_id, "image")
    if isinstance(checked, dict):
        return checked
    conn = store.init_db()
    try:
        row = store.get_image_row(conn, checked)
    finally:
        conn.close()
    if not row:
        return _error("NOT_FOUND", "Image not found")
    path = _jailed_image_path(row["path"])
    if isinstance(path, dict):
        return path
    data = path.read_bytes()
    if len(data) > 2 * 1024 * 1024:
        return _error("TOO_LARGE", "Image is larger than 2 MB")
    return {
        "ok": True,
        "image_id": row["image_id"],
        "source_id": row["source_id"],
        "format": row["format"],
        "transcription": row["transcription"],
        "description": row["description"],
        "data": data,
    }


def get_recent_sources(limit: int = 10) -> dict:
    checked = _limit(limit)
    if isinstance(checked, dict):
        return checked
    items = hub.recent_pdf_sources(checked)
    conn = store.init_db()
    try:
        rows = conn.execute(
            """
            SELECT source_id, source_type, title, category, section, updated_at
            FROM sources
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (checked,),
        ).fetchall()
    finally:
        conn.close()
    items.extend(
        {
            "source_id": row["source_id"],
            "source_type": row["source_type"],
            "title": row["title"],
            "category": row["category"],
            "section": row["section"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    )
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"ok": True, "limit": checked, "sources": items[:checked]}
