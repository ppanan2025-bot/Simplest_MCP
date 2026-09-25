# Simplest_MCP

check your serve disk without terminal using

Read-only Python MCP server for a Hermes host on Hetzner. It reports disk and memory usage, lists workspace file names, and exposes a jailed knowledge-hub reader. It does not run shell commands and does not need sudo.

## Tools

Host status (workspace jail: `/home/hermes/workspace`):

- `get_server_status()` — host overview: hostname, OS, uptime, CPU, memory, disk, Docker counts.
- `get_cpu_usage()` — logical/physical CPUs, usage percent, load average.
- `get_memory_usage()` — total / used / available memory.
- `get_disk_usage()` — root filesystem (`/`) only. No caller-chosen paths.
- `list_project_files(path)` — file names only, jailed to `/home/hermes/workspace`.

Docker inspection (read-only, host MCP process):

- `list_containers()` — running and stopped containers.
- `get_container_status(container_name)` — one container; no env/secrets.
- `get_container_logs(container_name, lines=100)` — recent logs, 1–1000 lines.

These Docker tools do not start, stop, restart, remove, or exec.

Knowledge hub (root: `/opt/knowledge-hub/data`):

- `list_hub_files(path="", recursive=False)` — list hub files. Read-only.
- `get_file_metadata(path)` — size, type, timestamps. No file body.
- `get_latest_files(limit=10)` — newest files by mtime. Metadata only.
- `read_hub_file(path)` — UTF-8 text or extracted PDF text, max 2 MB.
- `search_hub(query)` — substring search over text files and PDF text, limited results.

OneNote (Microsoft Graph, read-only):

- `onenote_status()` — whether the MCP process is signed in. No tokens returned.
- `onenote_login()` — device-code login; returns a URL and user code, never the access token.
- `list_onenote_notebooks()` — notebooks for the signed-in account.
- `list_onenote_sections(notebook_id)` — sections in one notebook.
- `list_onenote_pages(section_id)` — pages in one section.
- `read_onenote_page(page_id)` — page body as text, plus attached handwriting/screenshot images. Read those images; do not call a separate vision tool.
- `search_onenote(query)` — find pages by title/search.

Requires `ONENOTE_CLIENT_ID` on the MCP process (Entra public client with `Notes.Read`). Tokens are stored under `~/.config/simplest-mcp/` and are gitignored.

Canvas (LMS, read-only, authenticated student submission):

- `get_incomplete_canvas_assignments(include_overdue=True, include_future=True, days_ahead=None)` — unfinished work across current courses, using the signed-in user's submission, not due dates alone.
- `get_canvas_assignment_status(course_id, assignment_id)` — one assignment's submit/grade/excused/missing state.

Requires `CANVAS_BASE_URL` and `CANVAS_ACCESS_TOKEN` (personal access token) on the MCP process. GET only; the token is never returned. Do not commit it.

Unified knowledge (PDFs from existing `knowledge.db` + OneNote index):

- `unified_search(query, limit=10, source_type="")` — one search over PDF text and OneNote.
- `get_source(source_id)` / `read_chunk(chunk_id)` / `get_surrounding_context(chunk_id)`
- `get_source_image(image_id)` — original OneNote image for vision.
- `get_recent_sources(limit=10)`
- `refresh_unified_index(max_pages=80)` — reindex OneNote; skips unchanged pages.

The hub helpers live in `knowledge_hub.py`. The reusable framework is documented in the `knowledge_MCP` repo. Hermes usage notes live in `knowledge_hub_skill`.

## Requirements

- Python 3.10+
- Run as the `hermes` user (no sudo)
- Official MCP Python SDK (`mcp`), FastMCP API

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python server.py
```

## Security

- No shell execution, no arbitrary commands
- No sudo, no writes, no delete, no rename, no chmod
- `get_disk_usage` is fixed to `/`; it does not take a path from Hermes
- Docker tools talk only to the host Docker API through hardcoded SDK calls
- The MCP process runs as `hermes` and is not automatically added to the docker group
- Workspace tools stay inside `/home/hermes/workspace`
- Hub tools stay inside `/opt/knowledge-hub/data` after resolving symlinks
- `read_hub_file` rejects files larger than 2 MB; PDFs return extracted text only, never bytes
- Do not put tokens, SSH keys, or `.env` files in this repo
- OneNote tokens stay in `~/.config/simplest-mcp/` (mode 600), not in git
