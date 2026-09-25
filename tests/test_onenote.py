from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import onenote


class OneNoteTests(unittest.TestCase):
    def test_split_and_render_inkml(self) -> None:
        inkml = """<?xml version="1.0" encoding="utf-8"?>
<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML">
  <inkml:definitions>
    <inkml:brush xml:id="br0">
      <inkml:brushProperty name="color" value="#C00000"/>
    </inkml:brush>
  </inkml:definitions>
  <inkml:trace brushRef="#br0">10 10, 20 12, 40 18, 80 40</inkml:trace>
</inkml:ink>"""
        body = (
            "--bound\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            "<html><body><!-- InkNode is not supported --><title>Tiny Python</title></body></html>\r\n"
            "--bound\r\nContent-Type: application/inkml+xml\r\n\r\n"
            f"{inkml}\r\n"
            "--bound--"
        )
        html, ink = onenote.split_onenote_content(body, "multipart/mixed; boundary=bound")
        self.assertIn("InkNode is not supported", html)
        self.assertIn("inkml:trace", ink)
        png = onenote.render_inkml_png(ink)
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_pressure_channel_is_not_used_as_xy(self) -> None:
        inkml = """<?xml version="1.0" encoding="utf-8"?>
<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML">
  <inkml:definitions>
    <inkml:context xml:id="ctxCoordinatesWithPressure">
      <inkml:inkSource xml:id="inkSrc">
        <inkml:traceFormat>
          <inkml:channel name="X" type="integer"/>
          <inkml:channel name="Y" type="integer"/>
          <inkml:channel name="F" type="integer"/>
        </inkml:traceFormat>
      </inkml:inkSource>
    </inkml:context>
  </inkml:definitions>
  <inkml:trace contextRef="#ctxCoordinatesWithPressure">10 20 9000, 40 20 12000, 80 20 8000</inkml:trace>
</inkml:ink>"""
        count, x_index, y_index = onenote._ink_channel_layout(
            __import__("xml.etree.ElementTree", fromlist=["ET"]).fromstring(inkml)
        )
        self.assertEqual((count, x_index, y_index), (3, 0, 1))
        points = onenote._trace_points(
            "10 20 9000, 40 20 12000, 80 20 8000",
            channels=count,
            x_index=x_index,
            y_index=y_index,
        )
        self.assertEqual(points, [(10.0, 20.0), (40.0, 20.0), (80.0, 20.0)])
        png = onenote.render_inkml_png(inkml)
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG"))
        from PIL import Image
        import io

        image = Image.open(io.BytesIO(png)).convert("L")
        dark = sum(1 for pixel in image.getdata() if pixel < 20)
        self.assertGreater(dark, 20)

    def test_single_point_trace_is_drawn(self) -> None:
        inkml = """<?xml version="1.0" encoding="utf-8"?>
<inkml:ink xmlns:inkml="http://www.w3.org/2003/InkML">
  <inkml:trace>40 40</inkml:trace>
</inkml:ink>"""
        png = onenote.render_inkml_png(inkml)
        self.assertIsNotNone(png)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_tile_ink_png_splits_tall_pages(self) -> None:
        from PIL import Image

        tall = Image.new("RGB", (400, 3000), (255, 255, 255))
        buf = __import__("io").BytesIO()
        tall.save(buf, format="PNG")
        tiles = onenote.tile_ink_png(buf.getvalue())
        self.assertGreater(len(tiles), 1)
        self.assertLessEqual(len(tiles), onenote.MAX_INK_TILES)
        for tile in tiles:
            self.assertTrue(tile.startswith(b"\x89PNG"))
        short = Image.new("RGB", (400, 200), (255, 255, 255))
        small = __import__("io").BytesIO()
        short.save(small, format="PNG")
        self.assertEqual(len(onenote.tile_ink_png(small.getvalue())), 1)

    def test_html_to_text(self) -> None:
        text = onenote.html_to_text("<html><body><h1>Week 1</h1><p>Stacks are LIFO.</p></body></html>")
        self.assertIn("Week 1", text)
        self.assertIn("Stacks are LIFO.", text)

    def test_html_to_text_includes_image_alt(self) -> None:
        html = (
            '<html><head><title></title></head><body>'
            '<img alt="The COALESCE () function returns the first value. SQL&#39;s NULLIF." />'
            "</body></html>"
        )
        text = onenote.html_to_text(html)
        self.assertIn("COALESCE", text)
        self.assertIn("NULLIF", text)
        self.assertIn("SQL's", text)

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
        self.assertEqual(result["image_count"], 0)
        self.assertEqual(result.get("image_files"), [])
        self.assertEqual(result.get("images_fetched"), 0)
        self.assertEqual(result.get("_image_blobs"), [])
        self.assertNotIn("SECRET-TOKEN", str(result))

    def test_graph_image_urls_only(self) -> None:
        ok = onenote.allowed_image_url(
            "https://graph.microsoft.com/v1.0/me/onenote/resources/0-abc/content"
        )
        self.assertIsNotNone(ok)
        self.assertIsNone(onenote.allowed_image_url("http://169.254.169.254/latest/meta-data/"))
        self.assertIsNone(onenote.allowed_image_url("https://evil.example/onenote/resources/x"))
        self.assertIsNone(onenote.allowed_image_url("cid:foo"))
        self.assertIsNone(onenote.allowed_image_url("../etc/passwd"))

    def test_read_page_fetches_images_for_vision(self) -> None:
        token = {"ok": True, "access_token": "SECRET-TOKEN"}
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
            "0000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
        )
        html = (
            '<html><body><img alt="handwriting note" '
            'src="https://graph.microsoft.com/v1.0/me/onenote/resources/0-abc/content" />'
            "</body></html>"
        )

        def fake_get(path, _token, params=None, extra_headers=None):
            if path.endswith("/content"):
                return {"ok": True, "html": html}
            return {
                "ok": True,
                "data": {
                    "id": "page1",
                    "title": "String agg",
                    "createdDateTime": "2026-01-01T00:00:00Z",
                    "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                },
            }

        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"ONENOTE_IMAGE_DIR": tmp}, clear=False):
                with mock.patch.object(onenote, "_with_token", return_value=token):
                    with mock.patch.object(onenote, "_graph_get", side_effect=fake_get):
                        with mock.patch.object(
                            onenote,
                            "_download_onenote_image",
                            return_value={"data": png, "format": "png"},
                        ):
                            result = onenote.read_onenote_page("page1")
            self.assertTrue(result["ok"])
            self.assertIn("handwriting note", result["text"])
            self.assertEqual(result["images_fetched"], 1)
            self.assertEqual(len(result["image_files"]), 1)
            self.assertEqual(len(result["_image_blobs"]), 1)
            self.assertNotIn("SECRET-TOKEN", str(result["image_files"]))
            saved = Path(tmp).joinpath("page1", "01.png")
            self.assertTrue(saved.is_file())
            self.assertEqual(saved.read_bytes(), png)


if __name__ == "__main__":
    unittest.main()
