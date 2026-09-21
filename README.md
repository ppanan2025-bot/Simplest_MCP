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
