from __future__ import annotations

import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from monitoring import docker as docker_tools
from monitoring import system as system_tools


class FakeContainer:
    def __init__(self, name: str, status: str = "running") -> None:
        self.name = name
        self.short_id = "abc123def456"
        self.id = "abc123def456999"
        self.status = status
        self.image = SimpleNamespace(tags=["knowledge-hub:local"])
        self.attrs = {
            "Created": "2026-09-18T03:38:00Z",
            "Config": {"Image": "knowledge-hub:local", "Env": ["KNOWLEDGE_HUB_API_KEY=secret"]},
            "State": {
                "Status": status,
                "Running": status == "running",
                "StartedAt": "2026-09-18T03:38:10Z",
                "RestartCount": 0,
                "Health": {"Status": "healthy"},
            },
            "NetworkSettings": {"Ports": {"8765/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8765"}]}},
        }
        self._logs = b"2026-09-21T00:00:00Z starting\n2026-09-21T00:00:01Z ready\n"

    def logs(self, tail: int, timestamps: bool, stdout: bool, stderr: bool) -> bytes:
        assert timestamps is True
        assert stdout is True
        assert stderr is True
        lines = self._logs.splitlines()
        return b"\n".join(lines[-tail:]) + b"\n"


class FakeClient:
    def __init__(self, containers: list[FakeContainer]) -> None:
        self._containers = {item.name: item for item in containers}

        class _Collection:
            def __init__(self, mapping: dict[str, FakeContainer]) -> None:
                self._mapping = mapping

            def list(self, all: bool = False) -> list[FakeContainer]:
                items = list(self._mapping.values())
                if all:
                    return items
                return [item for item in items if item.status == "running"]

            def get(self, name: str) -> FakeContainer:
                if name not in self._mapping:
                    raise RuntimeError(f"No such container: {name}")
                return self._mapping[name]

        self.containers = _Collection(self._containers)


class SystemTests(unittest.TestCase):
    def test_get_server_status(self) -> None:
        status = system_tools.get_host_overview(
            {"available": False, "running_containers": None, "code": "DOCKER_PERMISSION_DENIED"}
        )
        self.assertTrue(status["ok"])
        self.assertIn("hostname", status)
        self.assertIn("operating_system", status)
        self.assertIn("uptime", status)
        self.assertIn("cpu", status)
        self.assertIn("memory", status)
        self.assertIn("disk", status)
        self.assertEqual(status["disk"]["path"], "/")

    def test_get_cpu_usage(self) -> None:
        cpu = system_tools.get_cpu_usage()
        self.assertTrue(cpu["ok"])
        self.assertGreaterEqual(cpu["logical_cpus"], 1)
        self.assertIn("percent", cpu)

    def test_get_memory_usage(self) -> None:
        mem = system_tools.get_memory_usage()
        self.assertTrue(mem["ok"])
        self.assertGreater(mem["total_bytes"], 0)
        self.assertIn("total", mem)
        self.assertIn("used", mem)
        self.assertIn("available", mem)

    def test_get_disk_usage(self) -> None:
        disk = system_tools.get_disk_usage()
        self.assertTrue(disk["ok"])
        self.assertEqual(disk["path"], "/")
        self.assertGreater(disk["total_bytes"], 0)
        self.assertIn("percent", disk)


class DockerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.running = FakeContainer("knowledge-hub")
        self.stopped = FakeContainer("openclaw-openclaw-cli-1", status="exited")
        self.client = FakeClient([self.running, self.stopped])
        self.patcher = mock.patch.object(docker_tools, "_client", return_value=self.client)
        self.patcher.start()

    def tearDown(self) -> None:
        self.patcher.stop()

    def test_list_containers(self) -> None:
        result = docker_tools.list_containers()
        self.assertTrue(result["ok"])
        names = {item["name"] for item in result["containers"]}
        self.assertEqual(names, {"knowledge-hub", "openclaw-openclaw-cli-1"})
        hub = next(item for item in result["containers"] if item["name"] == "knowledge-hub")
        self.assertEqual(hub["id"], "abc123def456")
        self.assertTrue(hub["running"])

    def test_valid_get_container_status(self) -> None:
        result = docker_tools.get_container_status("knowledge-hub")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "knowledge-hub")
        self.assertEqual(result["health"], "healthy")
        self.assertEqual(result["restart_count"], 0)
        self.assertNotIn("Env", result)
        self.assertNotIn("env", result)
        dumped = str(result)
        self.assertNotIn("KNOWLEDGE_HUB_API_KEY", dumped)
        self.assertNotIn("secret", dumped)

    def test_invalid_container_name(self) -> None:
        result = docker_tools.get_container_status("bad name; rm -rf /")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_CONTAINER_NAME")

    def test_get_container_logs_default_limit(self) -> None:
        result = docker_tools.get_container_logs("knowledge-hub")
        self.assertTrue(result["ok"])
        self.assertEqual(result["lines_requested"], 100)
        self.assertGreaterEqual(result["lines_returned"], 1)

    def test_get_container_logs_maximum_limit(self) -> None:
        result = docker_tools.get_container_logs("knowledge-hub", lines=1000)
        self.assertTrue(result["ok"])
        self.assertEqual(result["lines_requested"], 1000)

    def test_reject_lines_above_maximum(self) -> None:
        result = docker_tools.get_container_logs("knowledge-hub", lines=1001)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "INVALID_LINES")

    def test_nonexistent_container(self) -> None:
        result = docker_tools.get_container_status("no-such-box")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CONTAINER_NOT_FOUND")
        logs = docker_tools.get_container_logs("no-such-box")
        self.assertEqual(logs["code"], "CONTAINER_NOT_FOUND")

    def test_no_write_operations_exposed(self) -> None:
        forbidden = docker_tools.exposed_write_operations()
        source = Path(__file__).resolve().parents[1] / "server.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        tool_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertTrue(forbidden.isdisjoint(tool_names))
        docker_source = Path(__file__).resolve().parents[1] / "monitoring" / "docker.py"
        text = docker_source.read_text(encoding="utf-8")
        self.assertNotIn("container.start(", text)
        self.assertNotIn("container.stop(", text)
        self.assertNotIn("container.remove(", text)
        self.assertNotIn(".exec_run(", text)


if __name__ == "__main__":
    unittest.main()
