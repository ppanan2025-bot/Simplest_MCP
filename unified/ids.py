"""Stable IDs for unified sources, chunks, and images.

These tokens come from unified tools. Do not treat them as filesystem paths.
"""

from __future__ import annotations

import re

SOURCE_TYPES = frozenset({"pdf", "onenote"})
ID_RE = re.compile(r"^(pdf|onenote|img):[A-Za-z0-9_=.:{}!%-]{1,1024}$")
HEX_RE = re.compile(r"^[a-f0-9]{16,32}$")


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def validate_id(value: str, kind: str) -> str | dict:
    item = (value or "").strip()
    if not item or not ID_RE.fullmatch(item):
        return _error("INVALID_ID", f"{kind} is not a valid unified id")
    prefix = item.split(":", 1)[0]
    expected = {"source": ("pdf", "onenote"), "chunk": ("pdf", "onenote"), "image": ("img",)}
    if prefix not in expected.get(kind, ()):
        return _error("INVALID_ID", f"{kind} must start with {'/'.join(expected[kind])}:")
    return item


def pdf_source_id(document_id: str) -> str:
    return f"pdf:{document_id}"


def pdf_chunk_id(chunk_id: str) -> str:
    return f"pdf:{chunk_id}"


def onenote_source_id(page_id: str) -> str:
    return f"onenote:{page_id}"


def onenote_chunk_id(page_id: str, position: int) -> str:
    return f"onenote:{page_id}:c{int(position)}"


def parse_prefixed(value: str) -> tuple[str, str]:
    prefix, rest = value.split(":", 1)
    return prefix, rest
