"""Hermes-owned SQLite for OneNote sources/chunks/images. PDFs stay in the hub DB."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

UNIFIED_DB = Path(
    os.environ.get(
        "UNIFIED_DB_PATH",
        str(Path.home() / ".config" / "simplest-mcp" / "unified.db"),
    )
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    title TEXT,
    category TEXT,
    section TEXT,
    source_ref TEXT,
    created_at TEXT,
    updated_at TEXT,
    content_hash TEXT,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,
    rowid_n INTEGER UNIQUE,
    source_id TEXT NOT NULL,
    text TEXT NOT NULL,
    section TEXT,
    page_title TEXT,
    image_id TEXT,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    embedding BLOB,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    title,
    section,
    tokenize = 'porter'
);

CREATE TABLE IF NOT EXISTS images (
    image_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    format TEXT NOT NULL,
    path TEXT NOT NULL,
    transcription TEXT,
    description TEXT,
    FOREIGN KEY (source_id) REFERENCES sources(source_id)
);

CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id, position);
CREATE INDEX IF NOT EXISTS idx_images_source ON images(source_id);
CREATE INDEX IF NOT EXISTS idx_sources_updated ON sources(updated_at);
"""


def db_path() -> Path:
    return Path(os.environ.get("UNIFIED_DB_PATH", str(UNIFIED_DB))).expanduser()


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> sqlite3.Connection:
    own = conn is None
    conn = conn or connect()
    conn.executescript(SCHEMA)
    conn.commit()
    if own:
        try:
            os.chmod(db_path(), 0o600)
        except OSError:
            pass
    return conn


def next_rowid(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(rowid_n), 0) + 1 AS n FROM chunks").fetchone()
    return int(row["n"])


def delete_source(conn: sqlite3.Connection, source_id: str) -> None:
    rows = conn.execute("SELECT rowid_n FROM chunks WHERE source_id = ?", (source_id,)).fetchall()
    for row in rows:
        conn.execute("DELETE FROM chunks_fts WHERE rowid = ?", (row["rowid_n"],))
    conn.execute("DELETE FROM images WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM chunks WHERE source_id = ?", (source_id,))
    conn.execute("DELETE FROM sources WHERE source_id = ?", (source_id,))


def get_source_row(conn: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sources WHERE source_id = ?", (source_id,)).fetchone()


def get_chunk_row(conn: sqlite3.Connection, chunk_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()


def get_image_row(conn: sqlite3.Connection, image_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM images WHERE image_id = ?", (image_id,)).fetchone()
