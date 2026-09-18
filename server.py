"""Read-only MCP server for a Hermes host.

Exposes disk/memory status and a jailed workspace file listing.
Does not run shell commands and does not require sudo.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from mcp.server.fastmcp import FastMCP

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


if __name__ == "__main__":
    mcp.run()
