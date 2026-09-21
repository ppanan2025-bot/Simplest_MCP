"""Read-only host system metrics. Never writes or runs a shell."""

from __future__ import annotations

import os
import platform
import socket
import time

import psutil

DISK_PATH = "/"


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def format_duration(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def get_cpu_usage() -> dict:
    logical = psutil.cpu_count(logical=True) or 0
    physical = psutil.cpu_count(logical=False)
    percent = float(psutil.cpu_percent(interval=0.1))
    load: dict | None
    try:
        one, five, fifteen = os.getloadavg()
        load = {"1m": round(one, 2), "5m": round(five, 2), "15m": round(fifteen, 2)}
    except OSError:
        load = None
    return {
        "ok": True,
        "logical_cpus": logical,
        "physical_cpus": physical,
        "percent": percent,
        "load_average": load,
    }


def get_memory_usage() -> dict:
    mem = psutil.virtual_memory()
    return {
        "ok": True,
        "total_bytes": mem.total,
        "used_bytes": mem.used,
        "available_bytes": mem.available,
        "percent": mem.percent,
        "total": format_bytes(mem.total),
        "used": format_bytes(mem.used),
        "available": format_bytes(mem.available),
    }


def get_disk_usage() -> dict:
    usage = psutil.disk_usage(DISK_PATH)
    percent = round((usage.used / usage.total) * 100, 2) if usage.total else 0.0
    return {
        "ok": True,
        "path": DISK_PATH,
        "filesystem": DISK_PATH,
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "percent": percent,
        "total": format_bytes(usage.total),
        "used": format_bytes(usage.used),
        "free": format_bytes(usage.free),
    }


def get_uptime() -> dict:
    boot = psutil.boot_time()
    seconds = max(time.time() - boot, 0)
    return {
        "boot_time_unix": boot,
        "uptime_seconds": int(seconds),
        "uptime": format_duration(seconds),
    }


def get_host_overview(docker_summary: dict | None = None) -> dict:
    cpu = get_cpu_usage()
    memory = get_memory_usage()
    disk = get_disk_usage()
    uname = platform.uname()
    overview = {
        "ok": True,
        "hostname": socket.gethostname(),
        "operating_system": f"{uname.system} {uname.release}",
        "platform": platform.platform(),
        "uptime": get_uptime(),
        "load_average": cpu.get("load_average"),
        "cpu": {
            "logical_cpus": cpu["logical_cpus"],
            "physical_cpus": cpu["physical_cpus"],
            "percent": cpu["percent"],
        },
        "memory": memory,
        "disk": disk,
        "docker": docker_summary
        or {"available": False, "running_containers": None, "code": "DOCKER_UNCHECKED"},
    }
    return overview
