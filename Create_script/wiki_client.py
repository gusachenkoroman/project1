"""
Shared helpers for the Yandex.Wiki backup tools.

* WikiClient      - thin wrapper over the Yandex.Wiki REST API (https://api.wiki.yandex.net/v1)
                    with retries for 429/5xx and network errors.
* EventLog        - structured JSON Lines log (one JSON object per line) + console output.
* ResultsFile     - "page URL - status = ok|error" file, written line by line as work progresses.
* path helpers    - slug <-> URL conversion and Windows-safe file names / long paths.

The same file is shipped in both Save_script/ and Create_script/ so each tool is self-contained.
API reference: https://yandex.ru/support/wiki/ru/api-ref/
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("The 'requests' package is required. Install it with:  py -m pip install -r requirements.txt\n")
    raise

DEFAULT_API_BASE = "https://api.wiki.yandex.net/v1"
DEFAULT_WIKI_BASE = "https://wiki.yandex.ru"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BODY_IN_LOG = 4000


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def run_stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------
class EventLog:
    """Writes one JSON object per line. Every line has: ts, level, event, and context fields."""

    LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}

    def __init__(self, path: str, console_level: str = "INFO", run_id: str = ""):
        self.path = path
        self.run_id = run_id
        self.console_level = self.LEVELS.get(console_level.upper(), 20)
        os.makedirs(os.path.dirname(long_path(path)), exist_ok=True)
        self._fh = open(long_path(path), "a", encoding="utf-8", newline="\n")
        self.counts = {"WARNING": 0, "ERROR": 0}

    def log(self, level: str, event: str, **fields: Any) -> None:
        level = level.upper()
        rec = {"ts": now_iso(), "level": level, "run_id": self.run_id, "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        self._fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        self._fh.flush()
        if level in self.counts:
            self.counts[level] += 1
        if self.LEVELS.get(level, 20) >= self.console_level:
            msg = fields.get("message") or ""
            page = fields.get("page_url") or fields.get("slug") or ""
            extra = ""
            if "http_status" in fields:
                extra = f" [HTTP {fields['http_status']}" + (f" {fields.get('error_code')}" if fields.get("error_code") else "") + "]"
            line = f"{rec['ts'][11:19]} {level:<7} {event:<22} {page} {msg}{extra}".rstrip()
            stream = sys.stderr if level in ("WARNING", "ERROR") else sys.stdout
            try:
                stream.write(line + "\n")
            except UnicodeEncodeError:  # old Windows consoles
                stream.write(line.encode("ascii", "replace").decode("ascii") + "\n")
            stream.flush()

    def debug(self, event: str, **f: Any) -> None:
        self.log("DEBUG", event, **f)

    def info(self, event: str, **f: Any) -> None:
        self.log("INFO", event, **f)

    def warning(self, event: str, **f: Any) -> None:
        self.log("WARNING", event, **f)

    def error(self, event: str, **f: Any) -> None:
        self.log("ERROR", event, **f)

    def exception(self, event: str, exc: BaseException, **f: Any) -> None:
        data = dict(f)
        data.setdefault("message", str(exc))
        data["exception_type"] = type(exc).__name__
        if isinstance(exc, WikiApiError):
            data.update(exc.to_log())
        data["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.log("ERROR", event, **data)

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


class ResultsFile:
    """`<page URL> - status = ok|error`, one line per page, flushed immediately."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(long_path(path)), exist_ok=True)
        self._fh = open(long_path(path), "w", encoding="utf-8", newline="\n")
        self.ok = 0
        self.error = 0

    def write(self, page_url: str, ok: bool) -> None:
        self._fh.write(f"{page_url} - status = {'ok' if ok else 'error'}\n")
        self._fh.flush()
        if ok:
            self.ok += 1
        else:
            self.error += 1

    def close(self) -> None:
        self._fh.close()


# --------------------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------------------
class WikiApiError(Exception):
    def __init__(self, method: str, url: str, http_status: Optional[int], message: str,
                 error_code: Optional[str] = None, details: Any = None, response_body: Optional[str] = None):
        super().__init__(message)
        self.method = method
        self.url = url
        self.http_status = http_status
        self.error_code = error_code
        self.details = details
        self.response_body = response_body

    def to_log(self) -> Dict[str, Any]:
        return {
            "http_method": self.method,
            "request_url": self.url,
            "http_status": self.http_status,
            "error_code": self.error_code,
            "error_details": self.details,
            "response_body": (self.response_body or "")[:MAX_BODY_IN_LOG] or None,
        }


class WikiClient:
    def __init__(self, token: str, org_id: str, org_header: str = "X-Org-Id", auth_scheme: str = "OAuth",
                 api_base: str = DEFAULT_API_BASE, timeout: float = 60.0, max_retries: int = 5,
                 log: Optional[EventLog] = None, verify_tls: bool = True):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.log = log
        self.session = requests.Session()
        self.session.verify = verify_tls
        self.session.headers.update({
            "Authorization": f"{auth_scheme} {token}",
            org_header: str(org_id),
            "Accept": "application/json",
            "User-Agent": "wiki-backup-tools/1.0",
        })

    # -- core ------------------------------------------------------------------------
    def request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
                json_body: Any = None, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
                stream: bool = False, expect_json: bool = True, context: Optional[Dict[str, Any]] = None):
        url = path if path.startswith("http") else f"{self.api_base}/{path.lstrip('/')}"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        ctx = context or {}
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                resp = self.session.request(method, url, params=params or None, json=json_body, data=data,
                                            headers=headers, timeout=self.timeout, stream=stream)
            except requests.RequestException as exc:
                elapsed = round((time.monotonic() - started) * 1000)
                if self.log:
                    self.log.warning("http_network_error", http_method=method, request_url=url, params=params,
                                     attempt=attempt, elapsed_ms=elapsed, message=f"{type(exc).__name__}: {exc}", **ctx)
                if attempt <= self.max_retries:
                    time.sleep(min(2 ** attempt, 60))
                    continue
                raise WikiApiError(method, url, None, f"Network error: {type(exc).__name__}: {exc}") from exc

            elapsed = round((time.monotonic() - started) * 1000)
            if self.log:
                self.log.debug("http", http_method=method, request_url=resp.url, http_status=resp.status_code,
                               attempt=attempt, elapsed_ms=elapsed, request_body=_short_json(json_body), **ctx)

            if resp.status_code in RETRY_STATUSES and attempt <= self.max_retries:
                delay = _retry_after(resp) or min(2 ** attempt, 60)
                if self.log:
                    self.log.warning("http_retry", http_method=method, request_url=resp.url,
                                     http_status=resp.status_code, attempt=attempt, retry_in_s=delay,
                                     response_body=_safe_text(resp)[:MAX_BODY_IN_LOG], **ctx)
                resp.close()
                time.sleep(delay)
                continue

            if resp.status_code >= 400:
                body = _safe_text(resp)
                code, msg, details = None, None, None
                try:
                    j = json.loads(body)
                    if isinstance(j, dict):
                        code, msg, details = j.get("error_code"), j.get("debug_message"), j.get("details")
                except ValueError:
                    pass
                raise WikiApiError(method, resp.url, resp.status_code,
                                   msg or f"HTTP {resp.status_code} {resp.reason}", code, details, body)

            if stream:
                return resp
            if not expect_json:
                return resp.content
            if not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"_raw": resp.text}

    def paginate(self, path: str, params: Optional[Dict[str, Any]] = None, page_size: int = 100,
                 context: Optional[Dict[str, Any]] = None) -> Iterator[Dict[str, Any]]:
        params = dict(params or {})
        params["page_size"] = page_size
        seen_cursors = set()
        while True:
            data = self.request("GET", path, params=params, context=context)
            for item in data.get("results") or []:
                yield item
            cursor = data.get("next_cursor")
            if not cursor or cursor in seen_cursors:
                return
            seen_cursors.add(cursor)
            params["cursor"] = cursor

    # -- pages -----------------------------------------------------------------------
    def get_page(self, p_slug: str, p_fields: Optional[List[str]] = None, p_raise_on_redirect: bool = False, **ctx):
        return self.request("GET", "pages", params={"slug": p_slug, "fields": ",".join(p_fields) if p_fields else None,
                                                     "raise_on_redirect": "true" if p_raise_on_redirect else None},
                            context=ctx)

    def get_page_by_id(self, p_page_id: int, p_fields: Optional[List[str]] = None, **ctx):
        return self.request("GET", f"pages/{p_page_id}", params={"fields": ",".join(p_fields) if p_fields else None},
                            context=ctx)

    def descendants(self, p_slug: str, **ctx) -> Iterator[Dict[str, Any]]:
        return self.paginate("pages/descendants", {"slug": p_slug}, context=ctx)

    def create_page(self, p_body: Dict[str, Any], p_is_silent: bool = True, **ctx):
        return self.request("POST", "pages", params={"is_silent": "true" if p_is_silent else None},
                            json_body=p_body, context=ctx)

    def update_page(self, p_page_id: int, p_body: Dict[str, Any], p_is_silent: bool = True, **ctx):
        return self.request("POST", f"pages/{p_page_id}", params={"is_silent": "true" if p_is_silent else None},
                            json_body=p_body, context=ctx)

    # -- grids -----------------------------------------------------------------------
    def page_grids(self, p_page_id: int, **ctx):
        return self.paginate(f"pages/{p_page_id}/grids", context=ctx)

    def get_grid(self, p_grid_id: str, **ctx):
        return self.request("GET", f"grids/{p_grid_id}", params={"fields": "attributes,user_permissions"}, context=ctx)

    def create_grid(self, p_page_id: int, p_title: str, **ctx):
        return self.request("POST", "grids", json_body={"title": p_title, "page": {"id": p_page_id}}, context=ctx)

    def add_columns(self, p_grid_id: str, p_columns: List[Dict[str, Any]], p_revision: Optional[str] = None, **ctx):
        p_body: Dict[str, Any] = {"columns": p_columns}
        if p_revision:
            p_body["revision"] = p_revision
        return self.request("POST", f"grids/{p_grid_id}/columns", json_body=p_body, context=ctx)

    def add_rows(self, p_grid_id: str, p_rows: List[Dict[str, Any]], p_revision: Optional[str] = None, **ctx):
        p_body: Dict[str, Any] = {"rows": p_rows}
        if p_revision:
            p_body["revision"] = p_revision
        return self.request("POST", f"grids/{p_grid_id}/rows", json_body=p_body, context=ctx)

    # -- attachments -----------------------------------------------------------------
    def attachments(self, p_page_id: int, **ctx):
        return self.paginate(f"pages/{p_page_id}/attachments", context=ctx)

    def download_attachment(self, p_page_id: int, p_file_id: int, p_dest_path: str, **ctx) -> int:
        resp = self.request("GET", f"pages/{p_page_id}/attachments/{p_file_id}/download", stream=True, context=ctx)
        size = 0
        tmp = p_dest_path + ".part"
        try:
            with open(long_path(tmp), "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 256):
                    if chunk:
                        fh.write(chunk)
                        size += len(chunk)
            os.replace(long_path(tmp), long_path(p_dest_path))
        finally:
            resp.close()
            if os.path.exists(long_path(tmp)):
                os.remove(long_path(tmp))
        return size

    def upload_file(self, p_file_path: str, p_file_name: str, p_chunk_size: int = 5 * 1024 * 1024, **ctx) -> str:
        size = os.path.getsize(long_path(p_file_path))
        sess = self.request("POST", "upload_sessions", json_body={"file_name": p_file_name, "file_size": size},
                            context=ctx)
        session_id = sess["session_id"]
        with open(long_path(p_file_path), "rb") as fh:
            part = 1
            while True:
                chunk = fh.read(p_chunk_size)
                if not chunk and part > 1:
                    break
                self.request("PUT", f"upload_sessions/{session_id}/upload_part", params={"part_number": part},
                             data=chunk, headers={"Content-Type": "application/octet-stream"}, context=ctx)
                part += 1
                if len(chunk) < p_chunk_size:
                    break
        self.request("POST", f"upload_sessions/{session_id}/finish", context=ctx)
        return session_id

    def attach_uploads(self, p_page_id: int, p_session_ids: List[str], **ctx):
        return self.request("POST", f"pages/{p_page_id}/attachments", json_body={"upload_sessions": p_session_ids},
                            context=ctx)

    # -- comments --------------------------------------------------------------------
    def comments(self, p_page_id: int, **ctx):
        return self.paginate(f"pages/{p_page_id}/comments", context=ctx)

    def create_comment(self, p_page_id: int, p_body: Dict[str, Any], **ctx):
        return self.request("POST", f"pages/{p_page_id}/comments", json_body=p_body, context=ctx)


def _retry_after(resp) -> Optional[float]:
    v = resp.headers.get("Retry-After")
    if not v:
        return None
    try:
        return min(float(v), 120.0)
    except ValueError:
        return None


def _safe_text(resp) -> str:
    try:
        return resp.text
    except Exception:
        return ""


def _short_json(obj: Any) -> Any:
    if obj is None:
        return None
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= 1000 else s[:1000] + "...(truncated)"


# --------------------------------------------------------------------------------------
# Slugs, URLs and file system paths
# --------------------------------------------------------------------------------------
def slug_from_input(value: str) -> str:
    """Accepts a full wiki URL (https://wiki.yandex.ru/a/b/) or a slug (a/b) and returns 'a/b'."""
    v = (value or "").strip()
    if "://" in v:
        v = urllib.parse.urlparse(v).path
    else:
        v = v.split("?", 1)[0].split("#", 1)[0]
    v = urllib.parse.unquote(v).strip().strip("/")
    v = re.sub(r"/{2,}", "/", v)
    return v


def wiki_base_from_input(value: str, default: str = DEFAULT_WIKI_BASE) -> str:
    v = (value or "").strip()
    if "://" in v:
        p = urllib.parse.urlparse(v)
        return f"{p.scheme}://{p.netloc}"
    return default


def page_url(wiki_base: str, slug: str) -> str:
    return f"{wiki_base.rstrip('/')}/{urllib.parse.quote(slug, safe='/-_.~')}/" if slug else wiki_base.rstrip("/") + "/"


_WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str, max_len: int = 120) -> str:
    """Make a single path component safe on Windows (and everywhere else)."""
    n = _WIN_BAD.sub("_", name or "_").rstrip(" .") or "_"
    stem = n.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        n = "_" + n
    if len(n) > max_len:
        root, ext = os.path.splitext(n)
        ext = ext[:20]
        n = root[: max_len - len(ext)] + ext
    return n


def long_path(path: str) -> str:
    """On Windows, prefix absolute paths with \\\\?\\ so paths longer than 260 chars work."""
    if os.name != "nt":
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(long_path(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(long_path(tmp), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, default=str)
    os.replace(long_path(tmp), long_path(path))


def read_json(path: str) -> Any:
    with open(long_path(path), "r", encoding="utf-8") as fh:
        return json.load(fh)


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(long_path(path)), exist_ok=True)
    with open(long_path(path), "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read_text(path: str) -> str:
    with open(long_path(path), "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def resolve_auth(args) -> Dict[str, str]:
    """Token / org id from CLI args or environment variables."""
    token = args.token or os.environ.get("YANDEX_WIKI_TOKEN")
    org_id = args.org_id or os.environ.get("YANDEX_WIKI_ORG_ID")
    cloud_org_id = args.cloud_org_id or os.environ.get("YANDEX_WIKI_CLOUD_ORG_ID")
    if not token:
        raise SystemExit("ERROR: OAuth/IAM token is missing. Pass --token or set YANDEX_WIKI_TOKEN.")
    if not (org_id or cloud_org_id):
        raise SystemExit("ERROR: organization id is missing. Pass --org-id (Yandex 360) or --cloud-org-id "
                         "(Yandex Cloud / Identity Hub), or set YANDEX_WIKI_ORG_ID / YANDEX_WIKI_CLOUD_ORG_ID.")
    if cloud_org_id:
        return {"token": token, "org_id": cloud_org_id, "org_header": "X-Cloud-Org-Id"}
    return {"token": token, "org_id": org_id, "org_header": "X-Org-Id"}


def add_common_args(parser) -> None:
    g = parser.add_argument_group("connection")
    g.add_argument("--token", help="OAuth token (or IAM token with --auth-scheme Bearer). Default: env YANDEX_WIKI_TOKEN")
    g.add_argument("--auth-scheme", default=os.environ.get("YANDEX_WIKI_AUTH_SCHEME", "OAuth"),
                   choices=["OAuth", "Bearer"], help="'OAuth' for OAuth tokens, 'Bearer' for IAM tokens (default OAuth)")
    g.add_argument("--org-id", help="Yandex 360 organization id (header X-Org-Id). Default: env YANDEX_WIKI_ORG_ID")
    g.add_argument("--cloud-org-id", help="Yandex Cloud organization id (header X-Cloud-Org-Id). "
                                          "Default: env YANDEX_WIKI_CLOUD_ORG_ID")
    g.add_argument("--api-base", default=os.environ.get("YANDEX_WIKI_API_BASE", DEFAULT_API_BASE),
                   help=f"API base URL (default {DEFAULT_API_BASE})")
    g.add_argument("--timeout", type=float, default=60.0, help="HTTP timeout in seconds (default 60)")
    g.add_argument("--retries", type=int, default=5, help="Retries for 429/5xx/network errors (default 5)")
    g.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                   help="Console verbosity. The JSONL log file always contains everything, including DEBUG.")
