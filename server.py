"""Read-only MCP server for a Hermes host.

Exposes host status, Docker inspection, a jailed workspace listing, and a
jailed knowledge-hub reader. Does not run shell commands and does not require sudo.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import knowledge_hub
import onenote
from monitoring import docker as docker_tools
from monitoring import system as system_tools

WORKSPACE_ROOT = Path("/home/hermes/workspace").resolve()
SERVER_DIR = Path(__file__).resolve().parent

# Stay in the server directory so FastMCP never reads another user's .env.
os.chdir(SERVER_DIR)

mcp = FastMCP("simplest-mcp")


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
    """Get a read-only overview of the host server, including CPU, memory, disk and Docker status.

    Use this first when Hermes, running in Docker, needs a quick look at the
    Hetzner host. It does not change the system.
    """
    return system_tools.get_host_overview(docker_tools.docker_summary())


@mcp.tool()
def get_cpu_usage() -> dict:
    """Get read-only CPU information for the Hetzner host.

    Use this when Hermes needs logical/physical CPU count, usage percent, or
    load average. It does not run a shell.
    """
    return system_tools.get_cpu_usage()


@mcp.tool()
def get_memory_usage() -> dict:
    """Get read-only memory usage for the Hetzner host.

    Use this when Hermes needs total, used, and available memory. Values are
    returned in bytes and as human-readable strings.
    """
    return system_tools.get_memory_usage()


@mcp.tool()
def get_disk_usage() -> dict:
    """Get read-only disk usage for the host root filesystem.

    Use this when Hermes needs total, used, and free space. The path is fixed
    to `/` and cannot be chosen by the model.
    """
    return system_tools.get_disk_usage()


@mcp.tool()
def list_containers() -> dict:
    """List Docker containers visible to the host MCP process.

    Use this to see running and stopped containers on the Hetzner host. This
    is read-only: it cannot start, stop, restart, or remove containers.
    """
    return docker_tools.list_containers()


@mcp.tool()
def get_container_status(container_name: str) -> dict:
    """Inspect one Docker container on the Hetzner host.

    Use this when Hermes needs status, health, image, or ports for a named
    container. The name is validated. This cannot exec into the container or
    change its state. Environment variables and secrets are not returned.
    """
    return docker_tools.get_container_status(container_name)


@mcp.tool()
def get_container_logs(container_name: str, lines: int = 100) -> dict:
    """Read recent logs from a specific Docker container. Use this when diagnosing why a container or service is failing.

    Default is 100 lines; the maximum is 1000. The container name is validated.
    This cannot exec into the container or pass extra Docker CLI flags.
    """
    return docker_tools.get_container_logs(container_name, lines)


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


@mcp.tool()
def onenote_status() -> dict:
    """Check whether the host MCP is signed in to Microsoft OneNote.

    Use this before listing notebooks. It does not return tokens.
    """
    return onenote.auth_status()


@mcp.tool()
def onenote_login() -> dict:
    """Start or continue Microsoft device-code login for OneNote.

    Use this when OneNote tools return NOT_AUTHENTICATED. It returns a URL and
    a user_code. Open the URL, enter the code, then call this tool again.
    It never returns access tokens.
    """
    return onenote.onenote_login()


@mcp.tool()
def list_onenote_notebooks() -> dict:
    """List OneNote notebooks for the signed-in Microsoft account.

    Use this first when Hermes needs to find notes. Read-only.
    """
    return onenote.list_onenote_notebooks()


@mcp.tool()
def list_onenote_sections(notebook_id: str) -> dict:
    """List sections in one OneNote notebook.

    Use this after list_onenote_notebooks. notebook_id is validated. Read-only.
    """
    return onenote.list_onenote_sections(notebook_id)


@mcp.tool()
def list_onenote_pages(section_id: str) -> dict:
    """List pages in one OneNote section.

    Use this after list_onenote_sections. Read-only; does not return page bodies.
    """
    return onenote.list_onenote_pages(section_id)


@mcp.tool()
def read_onenote_page(page_id: str) -> dict:
    """Read one OneNote page as plain text.

    Use this when Hermes needs the contents of a note. HTML is converted to
    text. Tokens, passwords, and raw auth headers are not returned.
    """
    return onenote.read_onenote_page(page_id)


@mcp.tool()
def search_onenote(query: str) -> dict:
    """Search OneNote page titles for a query.

    Use this when the user asks which notes mention a topic. Query is 2–200
    characters. Read-only.
    """
    return onenote.search_onenote(query)


if __name__ == "__main__":
    mcp.run()
