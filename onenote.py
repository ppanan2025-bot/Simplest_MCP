"""Read-only Microsoft OneNote access via Graph. No writes, no shell."""

from __future__ import annotations

import io
import json
import os
import re
import socket
import time
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# MSAL adds openid/profile/offline_access itself; passing offline_access raises.
SCOPES = ["Notes.Read", "User.Read"]
# Personal (consumers) OneNote ids are OneDrive-style and include "!".
# Keep blocking path/shell characters such as / ; space | &.
ID_RE = re.compile(r"^[A-Za-z0-9_=.:{}!%-]{1,1024}$")
MAX_PAGE_CHARS = 200_000
HTTP_TIMEOUT_SECONDS = 15.0
MAX_INK_WIDTH = 2000
MAX_INK_HEIGHT = 4000
INK_TILE_HEIGHT = 1400
INK_TILE_OVERLAP = 160
MAX_INK_TILES = 5
MAX_PAGE_IMAGES = 6
MAX_IMAGE_BYTES = 2 * 1024 * 1024
WORKSPACE_ROOT = Path("/home/hermes/workspace").resolve()
HERMES_WORKSPACE = "/workspace"
CONFIG_DIR = Path(os.environ.get("ONENOTE_CONFIG_DIR") or (Path.home() / ".config" / "simplest-mcp"))
TOKEN_PATH = CONFIG_DIR / "onenote_token.bin"
FLOW_PATH = CONFIG_DIR / "onenote_device_flow.json"
IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"RIFF", "webp"),
)


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": message, "code": code}


def _client_id() -> str:
    return os.environ.get("ONENOTE_CLIENT_ID", "").strip()


def _tenant() -> str:
    return os.environ.get("ONENOTE_TENANT_ID", "common").strip() or "common"


def _authority() -> str:
    return f"https://login.microsoftonline.com/{_tenant()}"


def _force_ipv4() -> None:
    """Avoid hanging IPv6 connects to login.microsoftonline.com on this host."""
    if getattr(socket.getaddrinfo, "_simplest_mcp_ipv4", False):
        return
    original = socket.getaddrinfo

    def ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
        return original(host, port, socket.AF_INET, type, proto, flags)

    ipv4_only._simplest_mcp_ipv4 = True  # type: ignore[attr-defined]
    socket.getaddrinfo = ipv4_only
    try:
        import urllib3.util.connection as urllib3_cn

        urllib3_cn.allowed_gai_family = lambda: socket.AF_INET
    except Exception:
        pass


_force_ipv4()


def _graph_client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT_SECONDS)


def _validate_id(value: str, field: str) -> str | dict:
    item = (value or "").strip()
    if not item or not ID_RE.fullmatch(item):
        return _error("INVALID_ID", f"{field} is not a valid OneNote id")
    return item


class _HTMLText(HTMLParser):
    """Personal OneNote pages often store the note as images; Graph puts OCR in img alt."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.image_count = 0
        self.image_urls: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if tag != "img":
            return
        self.image_count += 1
        ad = {key.lower(): (value or "") for key, value in attrs}
        alt = unescape(" ".join((ad.get("alt") or "").split()))
        if alt:
            self.parts.append(alt)
        for key in ("data-fullres-src", "data-src", "src"):
            url = (ad.get(key) or "").strip()
            if url:
                self.image_urls.append(url)
                break
        else:
            resource_id = (ad.get("data-id") or "").strip()
            if resource_id:
                self.image_urls.append(
                    f"{GRAPH_BASE}/me/onenote/resources/{quote(resource_id, safe='')}/content"
                )

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self.parts.append(text)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    parser.feed(html or "")
    parser.close()
    return "\n".join(parser.parts)


def _local_name(tag: str) -> str:
    return (tag or "").split("}")[-1].lower()


def split_onenote_content(body: str, content_type: str = "") -> tuple[str, str]:
    """Split Graph page content into HTML and InkML parts."""
    text = body or ""
    ctype = (content_type or "").lower()
    html = text
    inkml = ""
    first_line = text.splitlines()[0].strip() if text else ""
    if "multipart" in ctype or first_line.startswith("--"):
        boundary = first_line if first_line.startswith("--") else ""
        if boundary:
            html_part = ""
            ink_part = ""
            for raw_part in text.split(boundary):
                part = raw_part.strip().strip("-")
                if not part:
                    continue
                header, sep, payload = part.partition("\r\n\r\n")
                if not sep:
                    header, sep, payload = part.partition("\n\n")
                blob = payload or part
                head = header.lower()
                if "inkml" in head or "<?xml" in blob[:80]:
                    ink_part = blob
                elif "html" in head or "<html" in blob.lower():
                    html_part = blob
            html = html_part or html
            inkml = ink_part
    if not inkml and "<inkml:ink" in text:
        start = text.find("<?xml")
        if start < 0:
            start = text.find("<inkml:ink")
        end = text.rfind("</inkml:ink>")
        if start >= 0 and end > start:
            inkml = text[start : end + len("</inkml:ink>")]
    return html, inkml


def _parse_hex_color(value: str) -> tuple[int, int, int]:
    raw = (value or "").strip()
    if raw.startswith("#"):
        digits = raw[1:]
        if len(digits) == 8:
            digits = digits[2:]
        if len(digits) == 6:
            return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)
    return {
        "red": (192, 0, 0),
        "black": (32, 32, 32),
        "blue": (31, 78, 121),
        "green": (0, 128, 0),
    }.get(raw.lower(), (32, 32, 32))


def _trace_points(
    text: str,
    channels: int = 2,
    x_index: int = 0,
    y_index: int = 1,
) -> list[tuple[float, float]]:
    nums = [float(item) for item in re.findall(r"-?\d+(?:\.\d+)?", text or "")]
    if channels < 2:
        channels = 2
    if x_index < 0 or y_index < 0 or x_index >= channels or y_index >= channels:
        x_index, y_index = 0, 1
    points: list[tuple[float, float]] = []
    for index in range(0, len(nums) - y_index, channels):
        points.append((nums[index + x_index], nums[index + y_index]))
    return points


def _ink_channel_layout(root: ET.Element) -> tuple[int, int, int]:
    """Return (channel_count, x_index, y_index) from InkML traceFormat.

    OneNote handwriting uses X,Y,F (pressure). Treating F as a coordinate
    fills the page with a dark scribble.
    """
    names: list[str] = []
    for el in root.iter():
        if _local_name(el.tag) != "channel":
            continue
        name = (el.get("name") or "").strip().upper()
        if name:
            names.append(name)
    if not names:
        return 2, 0, 1
    x_index = names.index("X") if "X" in names else 0
    y_index = names.index("Y") if "Y" in names else 1
    return max(len(names), 2), x_index, y_index


def render_inkml_png(inkml: str) -> bytes | None:
    """Rasterize OneNote InkML strokes so Hermes can see handwriting."""
    markup = (inkml or "").strip()
    if not markup or "<ink" not in markup.lower():
        return None
    start = markup.find("<?xml")
    if start < 0:
        start = markup.lower().find("<ink")
    end = markup.lower().rfind("</inkml:ink>")
    if end < 0:
        end = markup.lower().rfind("</ink>")
    if start >= 0 and end > start:
        close = markup.find(">", end)
        markup = markup[start : close + 1 if close > end else end + 12]
    try:
        root = ET.fromstring(markup)
    except ET.ParseError:
        return None

    channels, x_index, y_index = _ink_channel_layout(root)
    brushes: dict[str, tuple[tuple[int, int, int], int]] = {}
    for el in root.iter():
        if _local_name(el.tag) != "brush":
            continue
        brush_id = el.get("{http://www.w3.org/XML/1998/namespace}id") or el.get("id") or ""
        color = (32, 32, 32)
        himetric_width = 50.0
        for prop in el:
            if _local_name(prop.tag) != "brushproperty":
                continue
            name = (prop.get("name") or "").lower()
            value = prop.get("value") or ""
            if name == "color":
                color = _parse_hex_color(value)
            elif name == "width":
                try:
                    himetric_width = max(10.0, float(value))
                except ValueError:
                    himetric_width = 50.0
        if brush_id:
            brushes[brush_id] = (color, himetric_width)
            brushes[f"#{brush_id}"] = (color, himetric_width)

    strokes: list[tuple[list[tuple[float, float]], tuple[int, int, int], float]] = []
    for el in root.iter():
        if _local_name(el.tag) != "trace":
            continue
        points = _trace_points(el.text or "", channels=channels, x_index=x_index, y_index=y_index)
        if len(points) < 2:
            continue
        ref = el.get("brushRef") or ""
        color, himetric_width = brushes.get(ref, ((32, 32, 32), 50.0))
        strokes.append((points, color, himetric_width))
    if not strokes:
        return None

    xs = [x for points, _, _ in strokes for x, _ in points]
    ys = [y for points, _, _ in strokes for _, y in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1.0)
    span_y = max(max_y - min_y, 1.0)
    pad = 24
    scale = min((MAX_INK_WIDTH - 2 * pad) / span_x, (MAX_INK_HEIGHT - 2 * pad) / span_y)
    width = max(64, min(MAX_INK_WIDTH, int(span_x * scale) + 2 * pad))
    height = max(64, min(MAX_INK_HEIGHT, int(span_y * scale) + 2 * pad))

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    image = Image.new("RGB", (width, height), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for points, color, himetric_width in strokes:
        mapped = [
            (int((x - min_x) * scale) + pad, int((y - min_y) * scale) + pad)
            for x, y in points
        ]
        pen = max(2, min(8, int(round(himetric_width * scale)) or 2))
        draw.line(mapped, fill=color, width=pen, joint="curve")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def tile_ink_png(png: bytes) -> list[bytes]:
    """Split a tall ink page so vision can read it without timing out."""
    if not png:
        return []
    try:
        from PIL import Image
    except ImportError:
        return [png]
    try:
        image = Image.open(io.BytesIO(png)).convert("RGB")
    except Exception:
        return [png]
    width, height = image.size
    if height <= INK_TILE_HEIGHT + 80:
        return [png]
    tiles: list[bytes] = []
    step = max(200, INK_TILE_HEIGHT - INK_TILE_OVERLAP)
    top = 0
    while top < height and len(tiles) < MAX_INK_TILES:
        remaining = height - top
        if remaining <= INK_TILE_HEIGHT + 40 or len(tiles) == MAX_INK_TILES - 1:
            crop = image.crop((0, top, width, height))
            top = height
        else:
            crop = image.crop((0, top, width, top + INK_TILE_HEIGHT))
            top += step
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        tiles.append(buf.getvalue())
    return tiles


def _image_host_allowed(host: str, *, initial: bool) -> bool:
    host = (host or "").lower().rstrip(".")
    if initial:
        return host == "graph.microsoft.com"
    if host == "graph.microsoft.com":
        return True
    if host.endswith(".office.net") or host.endswith(".officeapps.live.com"):
        return True
    if host.endswith(".live.net"):
        return True
    return False


def allowed_image_url(url: str) -> str | None:
    """Allow only Graph OneNote resource URLs. Blocks SSRF to other hosts."""
    raw = (url or "").strip()
    if not raw or raw.lower().startswith(("cid:", "data:", "file:", "javascript:")):
        return None
    if raw.startswith("//"):
        raw = "https:" + raw
    if raw.startswith("/"):
        raw = "https://graph.microsoft.com" + raw
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    host = (parsed.hostname or "").lower()
    if not _image_host_allowed(host, initial=True):
        return None
    path = parsed.path.lower()
    if "onenote" not in path and "/resources/" not in path:
        return None
    return parsed.geturl()


def _sniff_image_format(data: bytes, content_type: str) -> str | None:
    ctype = (content_type or "").split(";")[0].strip().lower()
    mapped = {
        "image/png": "png",
        "image/jpeg": "jpeg",
        "image/jpg": "jpeg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(ctype)
    if mapped:
        return mapped
    if data.startswith(b"RIFF") and b"WEBP" in data[:16]:
        return "webp"
    for magic, fmt in IMAGE_MAGIC:
        if data.startswith(magic) and fmt != "webp":
            return fmt
    return None


def _image_root() -> Path:
    override = os.environ.get("ONENOTE_IMAGE_DIR")
    if override:
        return Path(override).resolve()
    resolved = (WORKSPACE_ROOT / "onenote-pages").resolve()
    if not resolved.is_relative_to(WORKSPACE_ROOT):
        return WORKSPACE_ROOT / "onenote-pages"
    return resolved


def _safe_page_dir(page_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", page_id)[:120] or "page"


def _hermes_path(path: Path) -> str:
    try:
        rel = path.resolve().relative_to(WORKSPACE_ROOT)
    except ValueError:
        return str(path)
    return f"{HERMES_WORKSPACE}/{rel.as_posix()}"


def _download_onenote_image(url: str, token: str) -> dict | None:
    allowed = allowed_image_url(url)
    if not allowed:
        return None
    headers = _headers(token)
    try:
        with _graph_client() as client:
            with client.stream("GET", allowed, headers=headers, follow_redirects=True) as response:
                final_host = urlparse(str(response.url)).hostname or ""
                if not _image_host_allowed(final_host, initial=False):
                    return None
                if response.status_code >= 400:
                    return None
                buf = bytearray()
                for chunk in response.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_IMAGE_BYTES:
                        return None
                data = bytes(buf)
                fmt = _sniff_image_format(data, response.headers.get("content-type", ""))
                if not fmt or not data:
                    return None
                return {"data": data, "format": fmt}
    except httpx.HTTPError:
        return None


def _save_page_images(page_id: str, blobs: list[dict]) -> list[str]:
    if not blobs:
        return []
    folder = _image_root() / _safe_page_dir(page_id)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(folder, 0o700)
    except OSError:
        return []
    saved: list[str] = []
    for index, blob in enumerate(blobs, start=1):
        path = folder / f"{index:02d}.{blob['format']}"
        try:
            path.write_bytes(blob["data"])
            os.chmod(path, 0o600)
        except OSError:
            continue
        saved.append(_hermes_path(path))
    return saved


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _load_cache():
    import msal

    cache = msal.SerializableTokenCache()
    if TOKEN_PATH.is_file():
        cache.deserialize(TOKEN_PATH.read_text(encoding="utf-8"))
    return cache


def _save_cache(cache) -> None:
    if not cache.has_state_changed and TOKEN_PATH.is_file():
        return
    _ensure_config_dir()
    TOKEN_PATH.write_text(cache.serialize(), encoding="utf-8")
    os.chmod(TOKEN_PATH, 0o600)


def _app(cache=None):
    import msal

    client_id = _client_id()
    if not client_id:
        return None
    cache = cache or _load_cache()
    return msal.PublicClientApplication(
        client_id,
        authority=_authority(),
        token_cache=cache,
        timeout=HTTP_TIMEOUT_SECONDS,
        instance_discovery=False,
    )


def _accounts(app) -> list:
    return app.get_accounts() if app is not None else []


def auth_status() -> dict:
    client_id = _client_id()
    if not client_id:
        return {
            "ok": False,
            "authenticated": False,
            "code": "AUTH_NOT_CONFIGURED",
            "error": "ONENOTE_CLIENT_ID is not set. Register an Entra public-client app with Notes.Read, then set that env var on the MCP process. Do not commit the id to git.",
        }
    if not TOKEN_PATH.is_file():
        return {
            "ok": True,
            "authenticated": False,
            "account_count": 0,
            "tenant": _tenant(),
        }
    try:
        app = _app()
        accounts = _accounts(app)
    except Exception as exc:
        return {
            "ok": False,
            "authenticated": False,
            "account_count": 0,
            "tenant": _tenant(),
            "code": "AUTH_UNAVAILABLE",
            "error": f"Could not reach Microsoft login: {exc}",
        }
    return {
        "ok": True,
        "authenticated": bool(accounts),
        "account_count": len(accounts),
        "tenant": _tenant(),
    }


def _acquire_silent() -> dict:
    app = _app()
    if app is None:
        return auth_status()
    cache = app.token_cache
    accounts = app.get_accounts()
    if not accounts:
        return _error("NOT_AUTHENTICATED", "OneNote is not signed in. Call onenote_login first.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    _save_cache(cache)
    if not result or "access_token" not in result:
        return _error("NOT_AUTHENTICATED", "OneNote token expired. Call onenote_login again.")
    return {"ok": True, "access_token": result["access_token"]}


def onenote_login() -> dict:
    """Start or continue Microsoft device-code login. Never returns the access token."""
    client_id = _client_id()
    if not client_id:
        return auth_status()

    try:
        cache = _load_cache()
        app = _app(cache)
        if app is None:
            return auth_status()
        accounts = app.get_accounts()
        silent = app.acquire_token_silent(SCOPES, account=accounts[0]) if accounts else None
        if silent and "access_token" in silent:
            _save_cache(cache)
            return {"ok": True, "authenticated": True, "message": "Already signed in to OneNote."}

        pending = None
        if FLOW_PATH.is_file():
            try:
                pending = json.loads(FLOW_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pending = None

        if pending and pending.get("device_code"):
            result = app.acquire_token_by_device_flow(
                {**pending, "expires_at": time.time() + 25}
            )
            if result and "access_token" in result:
                _save_cache(cache)
                try:
                    FLOW_PATH.unlink()
                except OSError:
                    pass
                return {
                    "ok": True,
                    "authenticated": True,
                    "message": "OneNote sign-in complete. You can list notebooks now.",
                }
            error = result.get("error") if isinstance(result, dict) else None
            if error in {"authorization_pending", "slow_down"} or not result:
                return {
                    "ok": True,
                    "authenticated": False,
                    "pending": True,
                    "verification_uri": pending.get("verification_uri") or "https://microsoft.com/devicelogin",
                    "user_code": pending.get("user_code"),
                    "message": "Still waiting. Open the URL, enter the code, then call onenote_login again.",
                }
            return _error(
                "AUTH_FAILED",
                result.get("error_description") or result.get("error") or "Device login failed",
            )

        flow = app.initiate_device_flow(scopes=SCOPES)
        if "user_code" not in flow:
            return _error("AUTH_FAILED", "Could not start Microsoft device login")
        _ensure_config_dir()
        FLOW_PATH.write_text(json.dumps(flow), encoding="utf-8")
        os.chmod(FLOW_PATH, 0o600)
        return {
            "ok": True,
            "authenticated": False,
            "pending": True,
            "verification_uri": flow.get("verification_uri") or "https://microsoft.com/devicelogin",
            "user_code": flow.get("user_code"),
            "message": flow.get("message")
            or "Open the URL, enter the user_code, then call onenote_login again.",
        }
    except Exception as exc:
        return _error("AUTH_UNAVAILABLE", f"Could not reach Microsoft login: {exc}")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _graph_get(path: str, token: str, params: dict | None = None, extra_headers: dict | None = None) -> dict:
    headers = _headers(token)
    if extra_headers:
        headers.update(extra_headers)
    url = f"{GRAPH_BASE}{path}"
    try:
        with _graph_client() as client:
            response = client.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        return _error("GRAPH_UNAVAILABLE", f"Could not reach Microsoft Graph: {exc}")
    if response.status_code == 401:
        return _error("NOT_AUTHENTICATED", "OneNote token was rejected. Call onenote_login again.")
    if response.status_code == 403:
        return _error("GRAPH_FORBIDDEN", "This app does not have Notes.Read permission.")
    if response.status_code == 404:
        return _error("NOT_FOUND", "OneNote item not found")
    if response.status_code >= 400:
        return _error("GRAPH_ERROR", f"Microsoft Graph returned HTTP {response.status_code}")
    content_type = response.headers.get("content-type", "")
    if (
        "text/html" in content_type
        or "multipart/" in content_type
        or "inkml" in content_type
        or path.endswith("/content")
    ):
        return {"ok": True, "html": response.text, "content_type": content_type}
    try:
        return {"ok": True, "data": response.json()}
    except json.JSONDecodeError:
        return _error("GRAPH_ERROR", "Microsoft Graph returned a non-JSON body")


def _with_token() -> dict:
    token = _acquire_silent()
    if not token.get("ok"):
        return token
    return token


def list_onenote_notebooks() -> dict:
    token = _with_token()
    if not token.get("ok") or "access_token" not in token:
        return token
    result = _graph_get(
        "/me/onenote/notebooks",
        token["access_token"],
        params={"$select": "id,displayName,createdDateTime,lastModifiedDateTime,isDefault"},
    )
    if not result.get("ok"):
        return result
    notebooks = []
    for item in (result.get("data") or {}).get("value") or []:
        notebooks.append(
            {
                "id": item.get("id"),
                "name": item.get("displayName"),
                "created": item.get("createdDateTime"),
                "modified": item.get("lastModifiedDateTime"),
                "is_default": item.get("isDefault"),
            }
        )
    return {"ok": True, "notebooks": notebooks}


def list_onenote_sections(notebook_id: str) -> dict:
    checked = _validate_id(notebook_id, "notebook_id")
    if isinstance(checked, dict):
        return checked
    token = _with_token()
    if not token.get("ok") or "access_token" not in token:
        return token
    encoded = quote(checked, safe="")
    result = _graph_get(
        f"/me/onenote/notebooks/{encoded}/sections",
        token["access_token"],
        params={"$select": "id,displayName,createdDateTime,lastModifiedDateTime"},
    )
    if not result.get("ok"):
        return result
    sections = []
    for item in (result.get("data") or {}).get("value") or []:
        sections.append(
            {
                "id": item.get("id"),
                "name": item.get("displayName"),
                "created": item.get("createdDateTime"),
                "modified": item.get("lastModifiedDateTime"),
            }
        )
    return {"ok": True, "notebook_id": checked, "sections": sections}


def list_onenote_pages(section_id: str) -> dict:
    checked = _validate_id(section_id, "section_id")
    if isinstance(checked, dict):
        return checked
    token = _with_token()
    if not token.get("ok") or "access_token" not in token:
        return token
    encoded = quote(checked, safe="")
    result = _graph_get(
        f"/me/onenote/sections/{encoded}/pages",
        token["access_token"],
        params={"$select": "id,title,createdDateTime,lastModifiedDateTime", "$top": "50"},
    )
    if not result.get("ok"):
        return result
    pages = []
    for item in (result.get("data") or {}).get("value") or []:
        pages.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "created": item.get("createdDateTime"),
                "modified": item.get("lastModifiedDateTime"),
            }
        )
    return {"ok": True, "section_id": checked, "pages": pages}


def read_onenote_page(page_id: str) -> dict:
    checked = _validate_id(page_id, "page_id")
    if isinstance(checked, dict):
        return checked
    token = _with_token()
    if not token.get("ok") or "access_token" not in token:
        return token
    encoded = quote(checked, safe="")
    meta = _graph_get(
        f"/me/onenote/pages/{encoded}",
        token["access_token"],
        params={"$select": "id,title,createdDateTime,lastModifiedDateTime"},
    )
    if not meta.get("ok"):
        return meta
    content = _graph_get(
        f"/me/onenote/pages/{encoded}/content",
        token["access_token"],
        params={"includeIDs": "true", "includeInkML": "true"},
    )
    if not content.get("ok"):
        return content
    html, inkml = split_onenote_content(content.get("html") or "", content.get("content_type") or "")
    if len(html) > MAX_PAGE_CHARS:
        html = html[:MAX_PAGE_CHARS]
    info = (meta.get("data") or {}) if isinstance(meta.get("data"), dict) else {}
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    blobs: list[dict] = []
    seen: set[str] = set()
    ink_png = render_inkml_png(inkml)
    ink_tiles = tile_ink_png(ink_png) if ink_png else []
    for tile in ink_tiles:
        if len(blobs) >= MAX_PAGE_IMAGES:
            break
        blobs.append({"data": tile, "format": "png"})
    for raw_url in parser.image_urls:
        if len(blobs) >= MAX_PAGE_IMAGES:
            break
        allowed = allowed_image_url(raw_url)
        if not allowed or allowed in seen:
            continue
        seen.add(allowed)
        fetched = _download_onenote_image(raw_url, token["access_token"])
        if fetched:
            blobs.append(fetched)
    image_files = _save_page_images(checked, blobs)
    text_parts = list(parser.parts)
    if ink_tiles and not any(part for part in text_parts if part and part != (info.get("title") or "")):
        text_parts.append(
            f"This page has handwritten ink in {len(ink_tiles)} image tile(s), top to bottom. "
            "Read each tile; do not send the whole page to a separate vision timeout."
        )
    return {
        "ok": True,
        "id": checked,
        "title": info.get("title"),
        "created": info.get("createdDateTime"),
        "modified": info.get("lastModifiedDateTime"),
        "text": "\n".join(text_parts),
        "image_count": parser.image_count + len(ink_tiles),
        "images_fetched": len(blobs),
        "has_ink": bool(ink_tiles),
        "ink_tiles": len(ink_tiles),
        "image_files": image_files,
        "html_truncated": len(content.get("html") or "") > MAX_PAGE_CHARS,
        "_image_blobs": blobs,
    }


def search_onenote(query: str) -> dict:
    needle = (query or "").strip()
    if len(needle) < 2:
        return _error("INVALID_QUERY", "query must be at least 2 characters")
    if len(needle) > 200:
        return _error("INVALID_QUERY", "query is too long")
    token = _with_token()
    if not token.get("ok") or "access_token" not in token:
        return token
    result = _graph_get(
        "/me/onenote/pages",
        token["access_token"],
        params={
            "$select": "id,title,createdDateTime,lastModifiedDateTime",
            "$top": "50",
            "search": needle,
        },
    )
    if not result.get("ok"):
        return result
    pages = []
    for item in (result.get("data") or {}).get("value") or []:
        pages.append(
            {
                "id": item.get("id"),
                "title": item.get("title"),
                "created": item.get("createdDateTime"),
                "modified": item.get("lastModifiedDateTime"),
            }
        )
    if pages and not any(needle.lower() in (item.get("title") or "").lower() for item in pages):
        # Graph may ignore `search` and return a page list; fall back to title match.
        titled = [
            item
            for item in pages
            if needle.lower() in (item.get("title") or "").lower()
        ]
        if titled:
            pages = titled
    return {"ok": True, "query": needle, "pages": pages[:20]}
