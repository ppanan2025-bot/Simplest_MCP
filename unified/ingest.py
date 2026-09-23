"""Ingest OneNote into the unified index. PDF chunks stay in the hub database."""

from __future__ import annotations

import hashlib

import onenote

from . import ids, store

MAX_INGEST_PAGES = 80
MAX_TYPED_CHUNK = 900


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def _split_typed(text: str) -> list[str]:
    body = (text or "").strip()
    if not body:
        return []
    if len(body) <= MAX_TYPED_CHUNK:
        return [body]
    parts: list[str] = []
    start = 0
    while start < len(body):
        parts.append(body[start : start + MAX_TYPED_CHUNK].strip())
        start += MAX_TYPED_CHUNK
    return [part for part in parts if part]


def _image_id(source_id: str, digest: str) -> str:
    return f"img:{hashlib.sha256(f'{source_id}:{digest}'.encode()).hexdigest()[:16]}"


def _hash_page(text: str, blobs: list[dict], modified: str) -> str:
    hasher = hashlib.sha256()
    hasher.update((modified or "").encode("utf-8", "replace"))
    hasher.update(b"\0")
    hasher.update((text or "").encode("utf-8", "replace"))
    for blob in blobs:
        hasher.update(b"\0")
        hasher.update(hashlib.sha256(blob.get("data") or b"").digest())
    return hasher.hexdigest()


def _insert_chunk(conn, source_id: str, text: str, section: str, title: str, image_id: str | None, position: int, kind: str) -> None:
    chunk_id = ids.onenote_chunk_id(ids.parse_prefixed(source_id)[1], position)
    rowid_n = store.next_rowid(conn)
    conn.execute(
        """
        INSERT INTO chunks (
            chunk_id, rowid_n, source_id, text, section, page_title, image_id, position, kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, rowid_n, source_id, text, section, title, image_id, position, kind),
    )
    conn.execute(
        "INSERT INTO chunks_fts(rowid, text, title, section) VALUES (?, ?, ?, ?)",
        (rowid_n, text, title or "", section or ""),
    )


def ingest_onenote_page(page: dict, notebook: str, section: str) -> dict:
    page_id = page.get("id") or ""
    checked = onenote._validate_id(page_id, "page_id")
    if isinstance(checked, dict):
        return checked
    source_id = ids.onenote_source_id(checked)
    modified = page.get("modified") or page.get("lastModifiedDateTime") or ""
    conn = store.init_db()
    existing = store.get_source_row(conn, source_id)
    if existing and existing["updated_at"] == modified and existing["content_hash"]:
        conn.close()
        return {"ok": True, "source_id": source_id, "skipped": True, "reason": "unchanged"}

    loaded = onenote.read_onenote_page(checked)
    if not loaded.get("ok"):
        conn.close()
        return loaded

    blobs = loaded.pop("_image_blobs", []) if isinstance(loaded, dict) else []
    text = loaded.get("text") or ""
    title = loaded.get("title") or page.get("title") or "Untitled"
    digest = _hash_page(text, blobs, modified)
    if existing and existing["content_hash"] == digest:
        conn.close()
        return {"ok": True, "source_id": source_id, "skipped": True, "reason": "same_hash"}

    store.delete_source(conn, source_id)
    conn.execute(
        """
        INSERT INTO sources (
            source_id, source_type, title, category, section, source_ref,
            created_at, updated_at, content_hash
        ) VALUES (?, 'onenote', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            title,
            notebook,
            section,
            checked,
            loaded.get("created") or "",
            modified,
            digest,
        ),
    )

    position = 0
    typed_kind = "handwriting" if loaded.get("image_count") else "typed"
    for part in _split_typed(text):
        _insert_chunk(conn, source_id, part, section, title, None, position, typed_kind)
        position += 1

    files = loaded.get("image_files") or []
    for index, blob in enumerate(blobs):
        data = blob.get("data") or b""
        fmt = blob.get("format") or "png"
        image_hash = hashlib.sha256(data).hexdigest()
        image_id = _image_id(source_id, image_hash)
        path = files[index] if index < len(files) else ""
        transcription = ""
        # Graph alt text is already in page text; keep a short image-specific description.
        description = f"OneNote image {index + 1} on {title}"
        if path:
            conn.execute(
                """
                INSERT OR REPLACE INTO images (
                    image_id, source_id, content_hash, format, path, transcription, description
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (image_id, source_id, image_hash, fmt, path, transcription, description),
            )
            _insert_chunk(
                conn,
                source_id,
                f"{description}. {text[:400]}".strip(),
                section,
                title,
                image_id,
                position,
                "image",
            )
            position += 1

    if position == 0:
        _insert_chunk(conn, source_id, title, section, title, None, 0, "typed")

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "source_id": source_id,
        "skipped": False,
        "title": title,
        "chunks": position,
        "images": len(blobs),
    }


def refresh_unified_index(max_pages: int = MAX_INGEST_PAGES) -> dict:
    """Pull OneNote pages into the unified index. Does not rewrite hub PDFs."""
    try:
        max_pages = int(max_pages)
    except (TypeError, ValueError):
        return _error("INVALID_LIMIT", "max_pages must be an integer")
    if max_pages < 1:
        return _error("INVALID_LIMIT", "max_pages must be at least 1")
    max_pages = min(max_pages, 200)

    store.init_db().close()
    notebooks = onenote.list_onenote_notebooks()
    if not notebooks.get("ok"):
        return notebooks

    scanned = 0
    ingested = 0
    skipped = 0
    errors = 0
    for notebook in notebooks.get("notebooks") or []:
        if scanned >= max_pages:
            break
        sections = onenote.list_onenote_sections(notebook.get("id") or "")
        if not sections.get("ok"):
            errors += 1
            continue
        for section in sections.get("sections") or []:
            if scanned >= max_pages:
                break
            pages = onenote.list_onenote_pages(section.get("id") or "")
            if not pages.get("ok"):
                errors += 1
                continue
            for page in pages.get("pages") or []:
                if scanned >= max_pages:
                    break
                scanned += 1
                result = ingest_onenote_page(
                    page,
                    notebook.get("name") or "",
                    section.get("name") or "",
                )
                if result.get("skipped"):
                    skipped += 1
                elif result.get("ok"):
                    ingested += 1
                else:
                    errors += 1

    return {
        "ok": True,
        "scanned": scanned,
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors,
        "truncated": scanned >= max_pages,
        "pdf_index": "existing knowledge.db (read-only)",
    }
