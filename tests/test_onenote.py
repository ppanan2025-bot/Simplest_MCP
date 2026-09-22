from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import onenote


class OneNoteTests(unittest.TestCase):
    def test_html_to_text(self) -> None:
        text = onenote.html_to_text("<html><body><h1>Week 1</h1><p>Stacks are LIFO.</p></body></html>")
        self.assertIn("Week 1", text)
        self.assertIn("Stacks are LIFO.", text)

    def test_status_without_client_id(self) -> None:
        with mock.patch.dict("os.environ", {"ONENOTE_CLIENT_ID": ""}, clear=False):
            status = onenote.auth_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["code"], "AUTH_NOT_CONFIGURED")
        self.assertNotIn("access_token", status)

    def test_status_configured_without_token_skips_network(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "ONENOTE_CLIENT_ID": "11111111-1111-1111-1111-111111111111",
                "ONENOTE_TENANT_ID": "consumers",
            },
            clear=False,
        ):
            with mock.patch.object(onenote, "TOKEN_PATH", Path("/tmp/simplest-mcp-no-onenote-token")):
                with mock.patch.object(onenote, "_app", side_effect=AssertionError("network")):
                    status = onenote.auth_status()
        self.assertTrue(status["ok"])
        self.assertFalse(status["authenticated"])
        self.assertEqual(status["tenant"], "consumers")
        self.assertNotIn("access_token", status)

    def test_invalid_ids_rejected(self) -> None:
        bad = onenote.list_onenote_sections("../etc/passwd")
        self.assertEqual(bad["code"], "INVALID_ID")
        bad_page = onenote.read_onenote_page("id; curl evil")
        self.assertEqual(bad_page["code"], "INVALID_ID")
        spaced = onenote.list_onenote_sections("0-ABC!123 more")
        self.assertEqual(spaced["code"], "INVALID_ID")

    def test_personal_onenote_ids_are_accepted(self) -> None:
        personal = "0-8A1E2FABC123DEF!123"
        checked = onenote._validate_id(personal, "notebook_id")
        self.assertEqual(checked, personal)
        token = {"ok": True, "access_token": "SECRET-TOKEN"}
        graph = {"ok": True, "data": {"value": []}}
        with mock.patch.object(onenote, "_with_token", return_value=token):
            with mock.patch.object(onenote, "_graph_get", return_value=graph) as graph_get:
                result = onenote.list_onenote_sections(personal)
        self.assertTrue(result["ok"])
        self.assertEqual(result["notebook_id"], personal)
        self.assertNotIn("SECRET-TOKEN", str(result))
        path = graph_get.call_args[0][0]
        self.assertIn("notebooks/", path)
        self.assertNotIn("..", path)
        self.assertNotIn(";", path)

    def test_search_query_too_short(self) -> None:
        result = onenote.search_onenote("a")
        self.assertEqual(result["code"], "INVALID_QUERY")

    def test_list_notebooks_does_not_leak_token(self) -> None:
        token = {"ok": True, "access_token": "SECRET-TOKEN"}
        graph = {
            "ok": True,
            "data": {
                "value": [
                    {
                        "id": "nb1",
                        "displayName": "COMP2022",
                        "createdDateTime": "2026-01-01T00:00:00Z",
                        "lastModifiedDateTime": "2026-01-02T00:00:00Z",
                        "isDefault": True,
                    }
                ]
            },
        }
        with mock.patch.object(onenote, "_with_token", return_value=token):
            with mock.patch.object(onenote, "_graph_get", return_value=graph):
                result = onenote.list_onenote_notebooks()
        self.assertTrue(result["ok"])
        self.assertEqual(result["notebooks"][0]["name"], "COMP2022")
        self.assertNotIn("access_token", result)
        self.assertNotIn("SECRET-TOKEN", str(result))

    def test_read_page_returns_text(self) -> None:
        token = {"ok": True, "access_token": "SECRET-TOKEN"}

        def fake_get(path, _token, params=None, extra_headers=None):
            if path.endswith("/content"):
                return {"ok": True, "html": "<p>Tiny Python</p>"}
            return {
                "ok": True,
                "data": {
                    "id": "page1",
                    "title": "Lecture",
                    "createdDateTime": "2026-01-01T00:00:00Z",
                    "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                },
            }

        with mock.patch.object(onenote, "_with_token", return_value=token):
            with mock.patch.object(onenote, "_graph_get", side_effect=fake_get):
                result = onenote.read_onenote_page("page1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Lecture")
        self.assertIn("Tiny Python", result["text"])
        self.assertNotIn("SECRET-TOKEN", str(result))


if __name__ == "__main__":
    unittest.main()
