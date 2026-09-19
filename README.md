# Simplest_MCP

check your serve disk without terminal using

Read-only Python MCP server for a Hermes host on Hetzner. It reports disk and memory usage, lists workspace file names, and exposes a jailed knowledge-hub reader. It does not run shell commands and does not need sudo.

## Tools

Host status (workspace jail: `/home/hermes/workspace`):

- `get_server_status()` — disk total / used / free, plus memory usage. Read-only.
- `get_disk_usage()` — disk total / used / free only.
- `list_project_files(path)` — file names only, jailed to `/home/hermes/workspace`.

Knowledge hub (root: `/opt/knowledge-hub/data`):

- `list_hub_files(path="", recursive=False)` — list hub files. Read-only.
- `get_file_metadata(path)` — size, type, timestamps. No file body.
- `get_latest_files(limit=10)` — newest files by mtime. Metadata only.
- `read_hub_file(path)` — UTF-8 text files only, max 2 MB.
- `search_hub(query)` — substring search over text files, limited results.

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
- Workspace tools stay inside `/home/hermes/workspace`
- Hub tools stay inside `/opt/knowledge-hub/data` after resolving symlinks
- `read_hub_file` rejects non-text formats and files larger than 2 MB
- Do not put tokens, SSH keys, or `.env` files in this repo
