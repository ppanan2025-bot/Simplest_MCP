"""Read-only MCP server for a Hermes host.

Exposes disk/memory status and a jailed workspace file listing.
Does not run shell commands and does not require sudo.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import knowledge_hub

WORKSPACE_ROOT = Path("/home/hermes/workspace").resolve()
DISK_PATH = "/"
SERVER_DIR = Path(__file__).resolve().parent

# Stay in the server directory so FastMCP never reads another user's .env.
os.chdir(SERVER_DIR)

mcp = FastMCP("simplest-mcp")


def _format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def _read_memory() -> dict[str, int | float | str]:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        parsed: dict[str, int] = {}
        for line in meminfo.read_text().splitlines():
            if ":" not in line:
                continue
            key, raw_value = line.split(":", 1)
            parts = raw_value.strip().split()
            if not parts:
                continue
            parsed[key] = int(parts[0]) * 1024

        total = parsed["MemTotal"]
        available = parsed.get("MemAvailable", parsed.get("MemFree", 0))
        used = max(total - available, 0)
    else:
        page_size = os.sysconf("SC_PAGE_SIZE")
        total = os.sysconf("SC_PHYS_PAGES") * page_size
        available = os.sysconf("SC_AVPHYS_PAGES") * page_size
        used = max(total - available, 0)

    percent_used = round((used / total) * 100, 2) if total else 0.0
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "percent_used": percent_used,
        "total": _format_bytes(total),
        "used": _format_bytes(used),
        "available": _format_bytes(available),
    }


def _resolve_workspace_path(path: str) -> Path:
    requested = (path or "").strip() or "."
    if "\x00" in requested:
        raise ValueError("Invalid path")

    candidate = Path(requested)
    if not candidate.is_absolute():
        candidate = WORKSPACE_ROOT / candidate

    resolved = candidate.resolve()
    if not resolved.is_relative_to(WORKSPACE_ROOT):
        raise PermissionError(
            "Access denied: path must stay inside /home/hermes/workspace"
        )
    return resolved


@mcp.tool()
def get_server_status() -> dict:
    """Return disk total/used/free space and memory usage. Read-only."""
    usage = shutil.disk_usage(DISK_PATH)
    return {
        "disk": {
            "path": DISK_PATH,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "total": _format_bytes(usage.total),
            "used": _format_bytes(usage.used),
            "free": _format_bytes(usage.free),
        },
        "memory": _read_memory(),
    }


@mcp.tool()
def get_disk_usage() -> dict:
    """Return disk total, used, and free space. Read-only."""
    return get_server_status()["disk"]


@mcp.tool()
def list_project_files(path: str = ".") -> dict:
    """Return file names under /home/hermes/workspace. Blocks path traversal."""
    target = _resolve_workspace_path(path)
    if not target.exists():
        raise FileNotFoundError("Path not found inside /home/hermes/workspace")

    if target.is_file():
        names = [target.name]
    else:
        names = sorted(entry.name for entry in target.iterdir())

    return {"files": names}


@mcp.tool()
def list_hub_files(path: str = "", recursive: bool = False) -> dict:
    """List knowledge-hub files under KNOWLEDGE_HUB_ROOT only.

    Use this first when Hermes needs to see which units, lectures, or notes
    exist in the local knowledge hub. Path is relative to the hub root.
    Set recursive=True to walk subfolders. This tool is read-only: it never
    writes, deletes, or runs a shell. It skips symlinks that escape the hub.
    """
    return knowledge_hub.list_hub_files(path, recursive)


@mcp.tool()
def get_file_metadata(path: str) -> dict:
    """Return size, type, and timestamps for one knowledge-hub path.

    Use this before read_hub_file to check whether a file is readable text
    or a PDF under 2 MB. Path must stay inside the hub root after symlink
    resolution. This tool does not read file contents.
    """
    return knowledge_hub.get_file_metadata(path)


@mcp.tool()
def get_latest_files(limit: int = 10) -> dict:
    """Return the most recently modified files in the knowledge hub.

    Use this when the user asks what was added or updated lately. Limit is
    clamped to 50. Results are metadata only (no file bodies). Read-only.
    """
    return knowledge_hub.get_latest_files(limit)


@mcp.tool()
def read_hub_file(path: str) -> dict:
    """Read one UTF-8 text file or extract text from a PDF in the knowledge hub.

    Use this after list_hub_files or search_hub when Hermes needs lecture or
    note content. Supported sources: text formats (md, txt, json, csv, yml,
    yaml, rst, log, py, html, xml, tex, toml, ini) and PDFs. Files larger
    than 2 MB are rejected. PDF bytes are never returned; only extracted
    text. Scanned image-only PDFs return EMPTY_PDF. Paths outside the hub
    root are denied. This tool never writes.
    """
    return knowledge_hub.read_hub_file(path)


@mcp.tool()
def search_hub(query: str) -> dict:
    """Search knowledge-hub text files and PDF text for a query string.

    Use this when the user asks which notes or lecture PDFs mention a topic.
    It scans supported text files and extractable PDFs, skips files over
    2 MB, and returns a limited set of path + line snippets. It does not
    run a shell. Query must be 2 to 200 characters.
    """
    return knowledge_hub.search_hub(query)


if __name__ == "__main__":
    mcp.run()
