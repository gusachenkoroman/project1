"""
Minimal in-memory imitation of the Yandex.Wiki API (only the endpoints the tools use).
Used by run_e2e_test.py; not needed for normal use of the tools.
"""

import json
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Store:
    def __init__(self):
        self.lock = threading.Lock()
        self.pages = {}        # id -> page dict
        self.next_id = 100
        self.grids = {}        # uuid -> grid
        self.files = {}        # page_id -> [attachment]
        self.blobs = {}        # file_id -> bytes
        self.uploads = {}      # session -> {name, data:bytearray}
        self.comments = {}     # page_id -> [comment]
        self.fail_slugs = set()       # GET by slug returns 500 forever
        self.fail_download = set()    # file ids failing on download
        self.calls = []

    def add_page(self, slug, title, content="", page_type="wysiwyg", **extra):
        with self.lock:
            pid = self.next_id
            self.next_id += 1
            self.pages[pid] = dict(id=pid, slug=slug, title=title, content=content, page_type=page_type,
                                   attributes={"created_at": "2026-01-01T00:00:00Z"}, breadcrumbs=[], redirect=None,
                                   access_policy={"access_type": "inherited"}, access_lists={"direct": []},
                                   owner={"user": {"username": "owner"}}, **extra)
            return pid

    def by_slug(self, slug):
        for p in self.pages.values():
            if p["slug"] == slug:
                return p
        return None


STORE = Store()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else (json.dumps(obj).encode() if obj is not None else b"")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, error_code, msg):
        self._send(code, {"error_code": error_code, "debug_message": msg, "details": None})

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _auth_ok(self):
        if not (self.headers.get("Authorization", "").startswith(("OAuth ", "Bearer "))
                and (self.headers.get("X-Org-Id") or self.headers.get("X-Cloud-Org-Id"))):
            self._err(401, "NOT_AUTHENTICATED", "no token")
            return False
        return True

    @staticmethod
    def _page_view(p, fields):
        out = {k: p[k] for k in ("id", "slug", "title", "page_type")}
        for f in fields:
            if f in p:
                out[f] = p[f]
        return out

    @staticmethod
    def _paged(items, q):
        size = int(q.get("page_size", ["50"])[0])
        start = int(q.get("cursor", ["0"])[0])
        chunk = items[start:start + size]
        nxt = str(start + size) if start + size < len(items) else None
        return {"results": chunk, "next_cursor": nxt, "prev_cursor": None}

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def handle_any(self, method):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path.replace("/v1/", "", 1).strip("/")
        body = self._body()
        STORE.calls.append((method, path, q))
        if not self._auth_ok():
            return
        s = STORE
        fields = (q.get("fields", [""])[0]).split(",") if q.get("fields") else []

        if method == "GET" and path == "pages":
            slug = q["slug"][0]
            if slug in s.fail_slugs:
                return self._err(500, "INTERNAL", "boom")
            p = s.by_slug(slug)
            return self._send(200, self._page_view(p, fields)) if p else self._err(404, "NOT_FOUND", "no page")
        if method == "GET" and path == "pages/descendants":
            slug = q["slug"][0]
            items = sorted([{"id": p["id"], "slug": p["slug"]} for p in s.pages.values()
                            if p["slug"].startswith(slug + "/")], key=lambda x: x["slug"])
            return self._send(200, self._paged(items, q))
        if method == "POST" and path == "pages":
            b = json.loads(body)
            if "page_type" in b:  # imitate an API that rejects unknown fields
                return self._err(422, "VALIDATION_ERROR", "extra field page_type")
            if s.by_slug(b["slug"]):
                return self._err(400, "SLUG_OCCUPIED", "exists")
            pid = s.add_page(b["slug"], b["title"], b.get("content", ""))
            return self._send(200, self._page_view(s.pages[pid], []))
        m = re.fullmatch(r"pages/(\d+)", path)
        if m:
            p = s.pages.get(int(m.group(1)))
            if not p:
                return self._err(404, "NOT_FOUND", "no page")
            if method == "POST":
                p.update(json.loads(body))
            return self._send(200, self._page_view(p, fields))
        m = re.fullmatch(r"pages/(\d+)/grids", path)
        if m:
            pid = int(m.group(1))
            items = [{"id": g["id"], "title": g["title"], "created_at": "x"} for g in s.grids.values()
                     if g["page"]["id"] == pid]
            return self._send(200, self._paged(items, q))
        m = re.fullmatch(r"pages/(\d+)/attachments", path)
        if m:
            pid = int(m.group(1))
            if method == "POST":
                for sid in json.loads(body)["upload_sessions"]:
                    up = s.uploads.pop(sid)
                    fid = len(s.blobs) + 1000
                    s.blobs[fid] = bytes(up["data"])
                    s.files.setdefault(pid, []).append({"id": fid, "name": up["name"], "size": str(len(up["data"])),
                                                        "is_downloadable": True, "check_status": "ready"})
                return self._send(200, {})
            return self._send(200, self._paged(s.files.get(pid, []), q))
        m = re.fullmatch(r"pages/(\d+)/attachments/(\d+)/download", path)
        if m:
            fid = int(m.group(2))
            if fid in s.fail_download:
                return self._err(403, "FORBIDDEN", "no access to file")
            return self._send(200, raw=s.blobs[fid], ctype="application/octet-stream")
        m = re.fullmatch(r"pages/(\d+)/comments", path)
        if m:
            pid = int(m.group(1))
            if method == "POST":
                b = json.loads(body)
                lst = s.comments.setdefault(pid, [])
                c = {"id": 5000 + len(lst) + pid * 10, "body": b["body"], "parent_id": b.get("parent_id"),
                     "created_at": "2026", "author": {"display_name": "Restorer"}}
                lst.append(c)
                return self._send(200, c)
            return self._send(200, self._paged(s.comments.get(pid, []), q))
        if method == "POST" and path == "grids":
            b = json.loads(body)
            gid = str(uuid.uuid4())
            s.grids[gid] = {"id": gid, "title": b["title"], "page": {"id": b["page"]["id"]},
                            "structure": {"columns": [], "default_sort": []}, "rows": [], "revision": "1"}
            return self._send(200, s.grids[gid])
        m = re.fullmatch(r"grids/([0-9a-f-]+)(/columns|/rows)?", path)
        if m:
            g = s.grids.get(m.group(1))
            if not g:
                return self._err(404, "NOT_FOUND", "no grid")
            if m.group(2) == "/columns":
                for c in json.loads(body)["columns"]:
                    g["structure"]["columns"].append(dict(c, id=c["slug"]))
                return self._send(200, {"revision": "2"})
            if m.group(2) == "/rows":
                cols = [c["slug"] for c in g["structure"]["columns"]]
                for r in json.loads(body)["rows"]:
                    g["rows"].append({"id": str(len(g["rows"]) + 1), "row": [r.get(c) for c in cols]})
                return self._send(200, {"revision": "3"})
            return self._send(200, g)
        if method == "POST" and path == "upload_sessions":
            b = json.loads(body)
            sid = str(uuid.uuid4())
            s.uploads[sid] = {"name": b["file_name"], "size": b["file_size"], "data": bytearray()}
            return self._send(200, {"session_id": sid, "status": "not_started"})
        m = re.fullmatch(r"upload_sessions/([0-9a-f-]+)/(upload_part|finish)", path)
        if m:
            up = s.uploads[m.group(1)]
            if m.group(2) == "upload_part":
                up["data"].extend(body)
            return self._send(200, {"session_id": m.group(1), "status": "finished"})
        return self._err(404, "NO_ROUTE", f"{method} {path}")


def start(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv
