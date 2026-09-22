"""Read-only Microsoft OneNote access via Graph. No writes, no shell."""

from __future__ import annotations

import json
import os
import re
import socket
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

import httpx

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
# MSAL adds openid/profile/offline_access itself; passing offline_access raises.
SCOPES = ["Notes.Read", "User.Read"]
# Personal (consumers) OneNote ids are OneDrive-style and include "!".
# Keep blocking path/shell characters such as / ; space | &.
ID_RE = re.compile(r"^[A-Za-z0-9_=.:{}!%-]{1,1024}$")
MAX_PAGE_CHARS = 200_000
HTTP_TIMEOUT_SECONDS = 10.0
CONFIG_DIR = Path(os.environ.get("ONENOTE_CONFIG_DIR") or (Path.home() / ".config" / "simplest-mcp"))
TOKEN_PATH = CONFIG_DIR / "onenote_token.bin"
FLOW_PATH = CONFIG_DIR / "onenote_device_flow.json"


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
    if "text/html" in content_type or path.endswith("/content"):
        return {"ok": True, "html": response.text}
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
    )
    if not content.get("ok"):
        return content
    html = content.get("html") or ""
    if len(html) > MAX_PAGE_CHARS:
        html = html[:MAX_PAGE_CHARS]
    info = (meta.get("data") or {}) if isinstance(meta.get("data"), dict) else {}
    parser = _HTMLText()
    parser.feed(html)
    parser.close()
    return {
        "ok": True,
        "id": checked,
        "title": info.get("title"),
        "created": info.get("createdDateTime"),
        "modified": info.get("lastModifiedDateTime"),
        "text": "\n".join(parser.parts),
        "image_count": parser.image_count,
        "html_truncated": len(content.get("html") or "") > MAX_PAGE_CHARS,
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
