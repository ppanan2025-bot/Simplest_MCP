"""Read-only knowledge-hub filesystem helpers.

All paths are jailed to KNOWLEDGE_HUB_ROOT. This module never runs a shell
and never writes, deletes, renames, or changes permissions.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

KNOWLEDGE_HUB_ROOT = Path("/opt/knowledge-hub/data").resolve()

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_LIST_ENTRIES = 500
MAX_LATEST_LIMIT = 50
DEFAULT_LATEST_LIMIT = 10
MAX_SEARCH_RESULTS = 20
MAX_SEARCH_SNIPPETS = 3
MAX_SEARCH_SCAN_FILES = 400
MAX_QUERY_LENGTH = 200
MIN_QUERY_LENGTH = 2
MAX_PDF_PAGES = 40
MAX_EXTRACTED_CHARS = 100_000
PDF_EXTENSION = ".pdf"

TEXT_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".json",
        ".csv",
        ".yml",
        ".yaml",
        ".rst",
        ".log",
        ".py",
        ".html",
        ".xml",
        ".tex",
        ".toml",
        ".ini",
    }
)


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def _rel(path: Path) -> str:
    return path.relative_to(KNOWLEDGE_HUB_ROOT).as_posix()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def resolve_hub_path(path: str | None) -> Path:
    """Return an absolute path inside KNOWLEDGE_HUB_ROOT.

    Resolves ``.`` / ``..`` and follows symlinks, then rejects anything
    that escapes the hub root.
    """
    requested = (path or "").strip()
    if "\x00" in requested:
        raise PermissionError("Invalid path")

    if requested in ("", ".", "/"):
        candidate = KNOWLEDGE_HUB_ROOT
    else:
        raw = Path(requested)
        candidate = raw if raw.is_absolute() else (KNOWLEDGE_HUB_ROOT / raw)

    resolved = candidate.resolve()
    if not resolved.is_relative_to(KNOWLEDGE_HUB_ROOT):
        raise PermissionError("Access denied: path must stay inside the knowledge hub root")
    return resolved


def _ensure_root() -> dict | None:
    if not KNOWLEDGE_HUB_ROOT.exists() or not KNOWLEDGE_HUB_ROOT.is_dir():
        return _error("HUB_UNAVAILABLE", "Knowledge hub root is not available on this host")
    return None


def _inside_root(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved.is_relative_to(KNOWLEDGE_HUB_ROOT)


def _is_supported_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_EXTENSIONS


def _is_pdf(path: Path) -> bool:
    return path.suffix.lower() == PDF_EXTENSION


def _is_readable_source(path: Path) -> bool:
    return _is_supported_text(path) or _is_pdf(path)


def _extract_pdf_text(path: Path) -> tuple[str, int]:
    """Return extracted text and page count. Read-only; no shell."""
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise RuntimeError("pypdf is not installed") from exc

    try:
        reader = PdfReader(str(path))
    except (OSError, PdfReadError, ValueError) as exc:
        raise ValueError(f"Could not open PDF: {exc}") from exc

    parts: list[str] = []
    page_count = 0
    for index, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        page_count = index
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        parts.append(f"[Page {index}]\n{text}")
    combined = "\n\n".join(parts).strip()
    if len(combined) > MAX_EXTRACTED_CHARS:
        combined = combined[:MAX_EXTRACTED_CHARS]
    return combined, page_count


def _read_source_text(path: Path) -> dict:
    if _is_pdf(path):
        try:
            content, page_count = _extract_pdf_text(path)
        except RuntimeError as exc:
            return _error("EXTRACTOR_UNAVAILABLE", str(exc))
        except ValueError as exc:
            return _error("UNSUPPORTED_TYPE", str(exc))
        if not content:
            return _error(
                "EMPTY_PDF",
                "PDF has no extractable text. It may be scanned images only.",
            )
        return {
            "ok": True,
            "encoding": "pdf-text",
            "content": content,
            "page_count": page_count,
        }
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return _error("UNSUPPORTED_TYPE", "File is not valid UTF-8 text")
    except OSError as exc:
        return _error("NOT_FOUND", str(exc))
    return {"ok": True, "encoding": "utf-8", "content": content}


def _entry_payload(path: Path) -> dict | None:
    if not _inside_root(path):
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    kind = "directory" if path.is_dir() else "file"
    payload = {
        "name": path.name,
        "path": _rel(path.resolve()),
        "type": kind,
        "size_bytes": st.st_size if kind == "file" else None,
        "modified_at": _iso(st.st_mtime),
    }
    if path.is_symlink():
        payload["symlink"] = True
    return payload


def list_hub_files(path: str = "", recursive: bool = False) -> dict:
    missing = _ensure_root()
    if missing:
        return missing
    try:
        target = resolve_hub_path(path)
    except PermissionError as exc:
        return _error("PATH_DENIED", str(exc))

    if not target.exists():
        return _error("NOT_FOUND", "Path not found inside the knowledge hub")
    if target.is_file():
        item = _entry_payload(target)
        return {"ok": True, "root": str(KNOWLEDGE_HUB_ROOT), "path": _rel(target), "files": [item] if item else []}
    if not target.is_dir():
        return _error("UNSUPPORTED_TYPE", "Path is not a file or directory")

    files: list[dict] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
            current = Path(dirpath)
            dirnames[:] = [
                name
                for name in dirnames
                if _inside_root(current / name) and not (current / name).is_symlink()
            ]
            for name in sorted(dirnames) + sorted(filenames):
                item = _entry_payload(current / name)
                if item:
                    files.append(item)
                if len(files) >= MAX_LIST_ENTRIES:
                    return {
                        "ok": True,
                        "root": str(KNOWLEDGE_HUB_ROOT),
                        "path": _rel(target),
                        "recursive": True,
                        "truncated": True,
                        "files": files,
                    }
    else:
        try:
            names = sorted(os.listdir(target))
        except OSError as exc:
            return _error("NOT_FOUND", str(exc))
        for name in names:
            item = _entry_payload(target / name)
            if item:
                files.append(item)
            if len(files) >= MAX_LIST_ENTRIES:
                return {
                    "ok": True,
                    "root": str(KNOWLEDGE_HUB_ROOT),
                    "path": _rel(target),
                    "recursive": False,
                    "truncated": True,
                    "files": files,
                }

    return {
        "ok": True,
        "root": str(KNOWLEDGE_HUB_ROOT),
        "path": _rel(target) if target != KNOWLEDGE_HUB_ROOT else "",
        "recursive": recursive,
        "truncated": False,
        "files": files,
    }


def get_file_metadata(path: str) -> dict:
    missing = _ensure_root()
    if missing:
        return missing
    if not (path or "").strip():
        return _error("INVALID_PATH", "path is required")
    try:
        target = resolve_hub_path(path)
    except PermissionError as exc:
        return _error("PATH_DENIED", str(exc))
    if not target.exists():
        return _error("NOT_FOUND", "Path not found inside the knowledge hub")

    try:
        st = target.stat()
    except OSError as exc:
        return _error("NOT_FOUND", str(exc))

    kind = "directory" if target.is_dir() else "file"
    return {
        "ok": True,
        "path": _rel(target),
        "name": target.name,
        "type": kind,
        "size_bytes": st.st_size if kind == "file" else None,
        "modified_at": _iso(st.st_mtime),
        "suffix": target.suffix.lower(),
        "is_text": kind == "file" and _is_supported_text(target),
        "is_pdf": kind == "file" and _is_pdf(target),
        "readable": kind == "file"
        and _is_readable_source(target)
        and st.st_size <= MAX_READ_BYTES,
        "symlink": target.is_symlink(),
    }


def get_latest_files(limit: int = DEFAULT_LATEST_LIMIT) -> dict:
    missing = _ensure_root()
    if missing:
        return missing
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return _error("INVALID_LIMIT", "limit must be an integer")
    if limit < 1:
        return _error("INVALID_LIMIT", "limit must be at least 1")
    limit = min(limit, MAX_LATEST_LIMIT)

    found: list[tuple[float, dict]] = []
    for dirpath, dirnames, filenames in os.walk(KNOWLEDGE_HUB_ROOT, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if _inside_root(current / name) and not (current / name).is_symlink()
        ]
        for name in filenames:
            path = current / name
            if path.is_symlink() and not _inside_root(path):
                continue
            item = _entry_payload(path)
            if not item or item["type"] != "file":
                continue
            found.append((path.stat().st_mtime, item))

    found.sort(key=lambda row: row[0], reverse=True)
    return {
        "ok": True,
        "root": str(KNOWLEDGE_HUB_ROOT),
        "limit": limit,
        "files": [item for _, item in found[:limit]],
    }


def read_hub_file(path: str) -> dict:
    missing = _ensure_root()
    if missing:
        return missing
    if not (path or "").strip():
        return _error("INVALID_PATH", "path is required")
    try:
        target = resolve_hub_path(path)
    except PermissionError as exc:
        return _error("PATH_DENIED", str(exc))
    if not target.exists():
        return _error("NOT_FOUND", "Path not found inside the knowledge hub")
    if not target.is_file():
        return _error("UNSUPPORTED_TYPE", "Path is not a file")
    if not _is_readable_source(target):
        return _error(
            "UNSUPPORTED_TYPE",
            "Only text files and PDFs can be read "
            f"({', '.join(sorted(TEXT_EXTENSIONS | {PDF_EXTENSION}))})",
        )

    size = target.stat().st_size
    if size > MAX_READ_BYTES:
        return _error("TOO_LARGE", "File is larger than 2 MB and cannot be read")

    loaded = _read_source_text(target)
    if not loaded.get("ok"):
        return loaded

    result = {
        "ok": True,
        "path": _rel(target),
        "size_bytes": size,
        "encoding": loaded["encoding"],
        "content": loaded["content"],
    }
    if "page_count" in loaded:
        result["page_count"] = loaded["page_count"]
    return result


def search_hub(query: str) -> dict:
    missing = _ensure_root()
    if missing:
        return missing
    needle = (query or "").strip()
    if len(needle) < MIN_QUERY_LENGTH:
        return _error("INVALID_QUERY", "query must be at least 2 characters")
    if len(needle) > MAX_QUERY_LENGTH:
        return _error("INVALID_QUERY", "query is too long")

    lowered = needle.lower()
    matches: list[dict] = []
    scanned = 0

    for dirpath, dirnames, filenames in os.walk(KNOWLEDGE_HUB_ROOT, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if _inside_root(current / name) and not (current / name).is_symlink()
        ]
        for name in filenames:
            path = current / name
            if not _inside_root(path) or not path.is_file() or not _is_readable_source(path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_READ_BYTES:
                continue
            scanned += 1
            if scanned > MAX_SEARCH_SCAN_FILES:
                return {
                    "ok": True,
                    "query": needle,
                    "truncated": True,
                    "matches": matches,
                }
            loaded = _read_source_text(path)
            if not loaded.get("ok"):
                continue
            text = loaded["content"]
            snippets: list[dict] = []
            for index, line in enumerate(text.splitlines(), start=1):
                if lowered in line.lower():
                    snippets.append(
                        {
                            "line": index,
                            "text": line.strip()[:240],
                        }
                    )
                    if len(snippets) >= MAX_SEARCH_SNIPPETS:
                        break
            if snippets:
                match = {
                    "path": _rel(path.resolve()),
                    "name": path.name,
                    "snippets": snippets,
                }
                if loaded.get("encoding") == "pdf-text":
                    match["source"] = "pdf"
                matches.append(match)
            if len(matches) >= MAX_SEARCH_RESULTS:
                return {
                    "ok": True,
                    "query": needle,
                    "truncated": True,
                    "matches": matches,
                }

    return {
        "ok": True,
        "query": needle,
        "truncated": False,
        "matches": matches,
    }
