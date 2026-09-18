# Simplest_MCP

check your serve disk without terminal using

Read-only Python MCP server for a Hermes host on Hetzner. It reports disk and memory usage and lists file names inside `/home/hermes/workspace`. It does not run shell commands and does not need sudo.

## Tools

- `get_server_status()` — disk total / used / free, plus memory usage. Read-only.
- `list_project_files(path)` — file names only, jailed to `/home/hermes/workspace`. Path traversal is rejected.

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

Stdio (typical Hermes / MCP host launch):

```bash
python server.py
```

## Hermes config example

Point Hermes at this server with stdio. Adjust the Python and script paths to match the Hetzner host:

```json
{
  "mcpServers": {
    "simplest-mcp": {
      "command": "/home/hermes/workspace/Simplest_MCP/.venv/bin/python",
      "args": ["/home/hermes/workspace/Simplest_MCP/server.py"]
    }
  }
}
```

## Security

- No shell execution, no arbitrary commands
- No sudo
- `list_project_files` resolves paths and allows only `/home/hermes/workspace` and below
- Do not put tokens, SSH keys, or `.env` files in this repo
