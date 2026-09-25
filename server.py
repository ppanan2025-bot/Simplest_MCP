"""Read-only MCP server for a Hermes host.

Exposes host status, Docker inspection, a jailed workspace listing, a
jailed knowledge-hub reader, and Canvas assignment status. Does not run
shell commands and does not require sudo.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image

import canvas
import knowledge_hub
import onenote
from monitoring import docker as docker_tools
from monitoring import system as system_tools
from unified import ingest as unified_ingest
from unified import search as unified_layer

WORKSPACE_ROOT = Path("/home/hermes/workspace").resolve()
SERVER_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Copy KEY=VALUE lines from .env into os.environ if the key is unset.

    FastMCP only applies FASTMCP_* from .env. Canvas and OneNote keys must
    be loaded here. Existing process env wins. Never logs values.
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


# Stay in the server directory so we never read another user's .env.
os.chdir(SERVER_DIR)
_load_dotenv(SERVER_DIR / ".env")

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
def read_onenote_page(page_id: str):
    """Read one OneNote page as text plus attached images.

    Use this when Hermes needs the contents of a note, including handwriting
    and screenshots. HTML text/alt is returned, and page images (ink tiles
    top to bottom) are attached to this result. Read those attached images.
    Do not call vision_analyze, tesseract, or schedule a cron retry.
    Tokens, passwords, and raw auth headers are not returned.
    """
    result = onenote.read_onenote_page(page_id)
    blobs = result.pop("_image_blobs", []) if isinstance(result, dict) else []
    images = [
        Image(data=blob["data"], format=blob["format"])
        for blob in blobs
        if isinstance(blob, dict) and blob.get("data") and blob.get("format")
    ]
    if not images:
        return result
    return [result, *images]


@mcp.tool()
def search_onenote(query: str) -> dict:
    """Search OneNote page titles for a query.

    Use this when the user asks which notes mention a topic. Query is 2–200
    characters. Read-only.
    """
    return onenote.search_onenote(query)


@mcp.tool()
def unified_search(query: str, limit: int = 10, source_type: str = "") -> dict:
    """Search Knowledge Hub PDFs and indexed OneNote with one query.

    Use this first for questions like BCNF, PDA, or normalization. It searches
    PDF text, OneNote typed text, handwriting transcriptions, and image
    descriptions. source_type may be pdf, onenote, or empty for both.
    Returns ids only — then read_chunk or get_source_image. No host paths.
    """
    return unified_layer.unified_search(query, limit=limit, source_type=source_type or None)


@mcp.tool()
def get_source(source_id: str) -> dict:
    """Return metadata for one unified source (pdf:... or onenote:...)."""
    return unified_layer.get_source(source_id)


@mcp.tool()
def read_chunk(chunk_id: str) -> dict:
    """Read one searchable chunk by id. Does not return a whole document."""
    return unified_layer.read_chunk(chunk_id)


@mcp.tool()
def get_surrounding_context(chunk_id: str) -> dict:
    """Return nearby chunks for the same source as chunk_id."""
    return unified_layer.get_surrounding_context(chunk_id)


@mcp.tool()
def get_source_image(image_id: str):
    """Return one indexed OneNote image for Hermes vision. image_id only."""
    result = unified_layer.get_source_image(image_id)
    data = result.pop("data", None) if isinstance(result, dict) else None
    if not data:
        return result
    return [result, Image(data=data, format=result.get("format") or "png")]


@mcp.tool()
def get_recent_sources(limit: int = 10) -> dict:
    """List recently updated PDF documents and indexed OneNote pages."""
    return unified_layer.get_recent_sources(limit)


@mcp.tool()
def get_incomplete_canvas_assignments(
    include_overdue: bool = True,
    include_future: bool = True,
    days_ahead: int | None = None,
) -> dict:
    """List Canvas assignments the signed-in student has not finished yet.

    Use this when the user asks which assignments are unfinished, overdue, or
    due this week. It reads the authenticated user's submission for each
    published assignment in current student courses. Completed means Canvas
    marks the work submitted, graded, pending review, or excused. Missing a
    grade after a submit is not unfinished. Assignments with submission type
    none are skipped. External-tool items are included only when Canvas marks
    them missing or when they are New Quizzes with no submit. Read-only.
    """
    return canvas.get_incomplete_canvas_assignments(
        include_overdue=include_overdue,
        include_future=include_future,
        days_ahead=days_ahead,
    )


@mcp.tool()
def get_canvas_assignment_status(course_id: str, assignment_id: str) -> dict:
    """Return my Canvas submission status for one assignment.

    Use this after identifying a course and assignment, for questions like
    whether a COMP2022 assignment was submitted. Uses include[]=submission
    for the authenticated user. Distinguishes unsubmitted, submitted but not
    graded, graded, excused, and missing. Does not infer completion from the
    due date alone. Read-only; never submits.
    """
    return canvas.get_canvas_assignment_status(course_id, assignment_id)


@mcp.tool()
def refresh_unified_index(max_pages: int = 80) -> dict:
    """Refresh the OneNote side of the unified index. Skips unchanged pages.

    Does not rewrite Knowledge Hub PDFs. Those stay in knowledge.db.
    """
    return unified_ingest.refresh_unified_index(max_pages=max_pages)


if __name__ == "__main__":
    mcp.run()
