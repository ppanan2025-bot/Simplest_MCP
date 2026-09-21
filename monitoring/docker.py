"""Read-only Docker inspection. No start/stop/exec/remove and no shell."""

from __future__ import annotations

import re
from typing import Any

CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MIN_LOG_LINES = 1
MAX_LOG_LINES = 1000
DEFAULT_LOG_LINES = 100

_FORBIDDEN_TOOLS = frozenset(
    {
        "start_container",
        "stop_container",
        "restart_container",
        "remove_container",
        "docker_exec",
        "run_command",
        "docker_command",
    }
)


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def validate_container_name(container_name: str) -> str | dict:
    name = (container_name or "").strip()
    if not name or not CONTAINER_NAME_RE.fullmatch(name):
        return _error(
            "INVALID_CONTAINER_NAME",
            "container_name must be a Docker name or ID "
            "(letters, digits, underscore, dot, hyphen; max 128 chars)",
        )
    return name


def validate_log_lines(lines: int) -> int | dict:
    try:
        value = int(lines)
    except (TypeError, ValueError):
        return _error("INVALID_LINES", "lines must be an integer")
    if value < MIN_LOG_LINES:
        return _error("INVALID_LINES", f"lines must be at least {MIN_LOG_LINES}")
    if value > MAX_LOG_LINES:
        return _error("INVALID_LINES", f"lines must be at most {MAX_LOG_LINES}")
    return value


def _docker_error(exc: Exception) -> dict:
    text = str(exc).lower()
    if "permission denied" in text or "connect" in text and "docker" in text:
        return _error(
            "DOCKER_PERMISSION_DENIED",
            "The MCP process cannot talk to Docker. The hermes user is not in "
            "the docker group and the socket was not changed. Ask the operator "
            "to add hermes to the docker group if host container inspection is required.",
        )
    if "no such container" in text or "not found" in text:
        return _error("CONTAINER_NOT_FOUND", "Container not found")
    return _error("DOCKER_UNAVAILABLE", "Docker is not available on this host")


def _client():
    try:
        import docker
        from docker.errors import DockerException
    except ImportError as exc:
        raise RuntimeError("docker SDK is not installed") from exc
    try:
        return docker.from_env(timeout=5)
    except DockerException as exc:
        raise RuntimeError(str(exc)) from exc


def _short_id(container: Any) -> str:
    raw = getattr(container, "short_id", None) or getattr(container, "id", "")
    return str(raw)[:12]


def _image_name(container: Any) -> str:
    tags = []
    image = getattr(container, "image", None)
    if image is not None:
        tags = list(getattr(image, "tags", None) or [])
    if tags:
        return tags[0]
    attrs = getattr(container, "attrs", {}) or {}
    config = attrs.get("Config") or {}
    return str(config.get("Image") or attrs.get("Image") or "")


def _created(container: Any) -> str | None:
    attrs = getattr(container, "attrs", {}) or {}
    return attrs.get("Created")


def _safe_ports(container: Any) -> dict:
    attrs = getattr(container, "attrs", {}) or {}
    ports = (attrs.get("NetworkSettings") or {}).get("Ports") or {}
    if not isinstance(ports, dict):
        return {}
    return ports


def _health(attrs: dict) -> str | None:
    state = attrs.get("State") or {}
    health = state.get("Health")
    if isinstance(health, dict):
        status = health.get("Status")
        return str(status) if status else None
    return None


def summarize_container(container: Any) -> dict:
    attrs = getattr(container, "attrs", {}) or {}
    state = attrs.get("State") or {}
    running = bool(state.get("Running", getattr(container, "status", "") == "running"))
    return {
        "name": (getattr(container, "name", "") or "").lstrip("/"),
        "id": _short_id(container),
        "image": _image_name(container),
        "status": getattr(container, "status", None) or state.get("Status"),
        "running": running,
        "created": _created(container),
    }


def detail_container(container: Any) -> dict:
    attrs = getattr(container, "attrs", {}) or {}
    state = attrs.get("State") or {}
    info = summarize_container(container)
    info.update(
        {
            "started_at": state.get("StartedAt"),
            "restart_count": state.get("RestartCount"),
            "health": _health(attrs),
            "ports": _safe_ports(container),
        }
    )
    return info


def docker_summary() -> dict:
    try:
        client = _client()
        running = client.containers.list(all=False)
        all_containers = client.containers.list(all=True)
    except RuntimeError as exc:
        err = _docker_error(exc)
        return {
            "available": False,
            "running_containers": None,
            "total_containers": None,
            "code": err["code"],
            "error": err["error"],
        }
    except Exception as exc:  # docker SDK raises APIError subclasses
        err = _docker_error(exc)
        return {
            "available": False,
            "running_containers": None,
            "total_containers": None,
            "code": err["code"],
            "error": err["error"],
        }
    return {
        "available": True,
        "running_containers": len(running),
        "total_containers": len(all_containers),
    }


def list_containers() -> dict:
    try:
        client = _client()
        containers = client.containers.list(all=True)
    except RuntimeError as exc:
        return _docker_error(exc)
    except Exception as exc:
        return _docker_error(exc)
    return {
        "ok": True,
        "containers": [summarize_container(item) for item in containers],
    }


def get_container_status(container_name: str) -> dict:
    checked = validate_container_name(container_name)
    if isinstance(checked, dict):
        return checked
    try:
        client = _client()
        container = client.containers.get(checked)
    except RuntimeError as exc:
        return _docker_error(exc)
    except Exception as exc:
        if exc.__class__.__name__ == "NotFound" or "no such container" in str(exc).lower():
            return _error("CONTAINER_NOT_FOUND", f"Container not found: {checked}")
        return _docker_error(exc)
    return {"ok": True, **detail_container(container)}


def get_container_logs(container_name: str, lines: int = DEFAULT_LOG_LINES) -> dict:
    checked = validate_container_name(container_name)
    if isinstance(checked, dict):
        return checked
    limit = validate_log_lines(lines)
    if isinstance(limit, dict):
        return limit
    try:
        client = _client()
        container = client.containers.get(checked)
        raw = container.logs(tail=limit, timestamps=True, stdout=True, stderr=True)
    except RuntimeError as exc:
        return _docker_error(exc)
    except Exception as exc:
        if exc.__class__.__name__ == "NotFound" or "not found" in str(exc).lower():
            return _error("CONTAINER_NOT_FOUND", f"Container not found: {checked}")
        return _docker_error(exc)
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    else:
        text = str(raw)
    log_lines = text.splitlines()
    return {
        "ok": True,
        "container": checked,
        "lines_requested": limit,
        "lines_returned": len(log_lines),
        "logs": log_lines,
    }


def exposed_write_operations() -> set[str]:
    """Names this module must never expose as MCP tools."""
    return set(_FORBIDDEN_TOOLS)
