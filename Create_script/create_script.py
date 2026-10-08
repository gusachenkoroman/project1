#!/usr/bin/env python3
"""
Create_script - re-create (publish) Yandex.Wiki pages from a backup made by Save_script.

    py create_script.py --StartPageToCreate https://wiki.yandex.ru/restore/team/ --WikiBackupPages D:\\WikiBackup

The backup's start page is created at StartPageToCreate, every subpage below it keeps its relative path.
Parents are always created before children. See README.md for details, logs and the results file.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Any, Dict, List, Optional

from wiki_client import (EventLog, ResultsFile, WikiApiError, WikiClient, add_common_args, long_path, page_url,
                         read_json, read_text, resolve_auth, run_stamp, slug_from_input, wiki_base_from_input)

PAGE_FILE = "@page.json"
CONTENT_FILE = "@content.md"
GRIDS_DIR = "@grids"
ATTACH_DIR = "@attachments"
COMMENTS_FILE = "@comments.json"
MANIFEST = "manifest.json"
ROWS_BATCH = 100


def map_slug(src_slug: str, src_root: str, dst_root: str) -> str:
    if src_slug == src_root:
        return dst_root
    tail = src_slug[len(src_root) + 1:] if src_root else src_slug
    return f"{dst_root}/{tail}" if dst_root else tail


def rewrite_links(content: str, old_root: str, new_root: str) -> str:
    """Rewrites links that point into the saved tree (/old/root/...) so they point into the new tree."""
    if not content or not old_root or old_root == new_root:
        return content
    pat = re.compile(r"(?<![\w.\-/])((?:https?://[^\s/)\]\"'<>]+)?/)" + re.escape(old_root)
                     + r"(?=[/)\]\s\"'#?|<>]|$)")
    return pat.sub(lambda m: m.group(1) + new_root, content)


def cell_for_api(value: Any, column: Dict[str, Any]) -> Any:
    """Converts a cell value as returned by GET /grids/{id} into what POST /grids/{id}/rows accepts."""
    ctype = column.get("type")

    def user_ident(u: Dict[str, Any]) -> Dict[str, Any]:
        ident = dict(u.get("identity") or {})
        if u.get("username"):
            ident["username"] = u["username"]
        return {k: v for k, v in ident.items() if v}

    if value is None:
        return None
    if ctype == "ticket_field":
        return None  # computed from Yandex Tracker, cannot be written
    if isinstance(value, dict):
        if "identity" in value or "username" in value:
            return [user_ident(value)]
        if "key" in value and "resolved" in value:
            return value["key"]
        if "key" in value and "display" in value:
            return value["key"]
        return value
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return [user_ident(v) for v in value if isinstance(v, dict)]
    return value


class Creator:
    def __init__(self, client: Optional[WikiClient], log: EventLog, results: ResultsFile, backup_dir: str,
                 wiki_base: str, src_root: str, dst_root: str, opts: argparse.Namespace):
        self.c = client
        self.log = log
        self.results = results
        self.backup_dir = backup_dir
        self.wiki_base = wiki_base
        self.src_root = src_root
        self.dst_root = dst_root
        self.opts = opts
        self.created: Dict[str, int] = {}          # src slug -> new page id
        self.redirects: List[Dict[str, Any]] = []  # pages to turn into redirects at the end

    def create_page(self, entry: Dict[str, Any]) -> bool:
        src_slug = entry["slug"]
        dst_slug = map_slug(src_slug, self.src_root, self.dst_root)
        url = page_url(self.wiki_base, dst_slug)
        pdir = os.path.join(self.backup_dir, entry["dir"])
        ctx = {"page_url": url, "slug": dst_slug, "source_slug": src_slug}
        self.log.info("page_create_start", **ctx, local_dir=pdir)
        errors: List[str] = []

        # 1. read local files
        try:
            meta = read_json(os.path.join(pdir, PAGE_FILE))
            content = read_text(os.path.join(pdir, CONTENT_FILE))
        except (OSError, ValueError) as exc:
            self.log.exception("backup_read_failed", exc, **ctx, step="read_backup")
            self.results.write(url, False)
            return False
        if not self.opts.no_rewrite_links:
            content = rewrite_links(content, self.src_root, self.dst_root)
        title = meta.get("title") or dst_slug.rsplit("/", 1)[-1]

        if self.opts.dry_run:
            self.log.info("dry_run_page", **ctx, title=title, page_type=meta.get("page_type"),
                          content_length=len(content), message=f"would create '{title}'")
            self.results.write(url, True)
            return True

        # 2. create (or reuse) the page
        try:
            page_id = self._create_or_reuse(dst_slug, title, content, meta.get("page_type"), ctx)
        except WikiApiError as exc:
            self.log.exception("page_create_failed", exc, **ctx, step="create_page")
            self.results.write(url, False)
            return False
        if page_id is None:  # existed and on-exists=error/skip; already reported
            return self._last_status
        self.created[src_slug] = page_id

        # 3. attachments
        if not self.opts.skip_attachments:
            errors += self._attachments(page_id, pdir, ctx)
        # 4. dynamic tables (new ids -> patch the {% wgrid id="..." %} macros in content)
        if not self.opts.skip_grids:
            grid_map, gerr = self._grids(page_id, pdir, ctx)
            errors += gerr
            new_content = content
            for old_id, new_id in grid_map.items():
                new_content = new_content.replace(str(old_id), str(new_id))
            if new_content != content:
                try:
                    self.c.update_page(page_id, {"content": new_content}, **ctx)
                    self.log.info("grid_links_updated", **ctx, grids=grid_map)
                except WikiApiError as exc:
                    self.log.exception("grid_links_update_failed", exc, **ctx, step="update_content")
                    errors.append(f"update content with new grid ids: {exc}")
        # 5. access policy
        if self.opts.restore_access:
            errors += self._access(page_id, meta, ctx)
        # 6. comments
        if self.opts.restore_comments:
            errors += self._comments(page_id, pdir, ctx)
        # 7. redirects are applied after all pages exist
        if meta.get("redirect"):
            self.redirects.append({"page_id": page_id, "ctx": ctx, "redirect": meta["redirect"]})

        ok = not errors
        self.results.write(url, ok)
        if ok:
            self.log.info("page_create_ok", **ctx, page_id=page_id)
        else:
            self.log.error("page_create_error", **ctx, page_id=page_id, errors=errors,
                           message=f"page exists, but {len(errors)} component(s) failed")
        return ok

    _last_status = True
    _send_page_type = True

    def _create_or_reuse(self, slug: str, title: str, content: str, page_type: Optional[str],
                         ctx: Dict[str, Any]) -> Optional[int]:
        existing = None
        try:
            existing = self.c.get_page(slug, **ctx)
        except WikiApiError as exc:
            if exc.http_status != 404:
                raise
        if existing:
            mode = self.opts.on_exists
            url = ctx["page_url"]
            if mode == "update":
                self.c.update_page(existing["id"], {"title": title, "content": content}, **ctx)
                self.log.info("page_updated_existing", **ctx, page_id=existing["id"])
                return existing["id"]
            if mode == "skip":
                self.log.warning("page_exists_skipped", **ctx, page_id=existing.get("id"),
                                 message="Page already exists, skipped (--on-exists skip)")
                self.results.write(url, True)
                self._last_status = True
                return None
            self.log.error("page_exists", **ctx, page_id=existing.get("id"),
                           message="Page already exists. Use --on-exists update|skip or another StartPageToCreate")
            self.results.write(url, False)
            self._last_status = False
            return None

        body = {"title": title, "slug": slug, "content": content}
        if page_type in ("page", "wysiwyg") and self._send_page_type:
            body_typed = dict(body, page_type=page_type)
            try:
                resp = self.c.create_page(body_typed, **ctx)
                return resp["id"]
            except WikiApiError as exc:
                if exc.http_status not in (400, 422):
                    raise
                self._send_page_type = False  # API does not accept page_type; stop sending it
                self.log.warning("page_create_retry_without_type", **ctx, message=str(exc), **exc.to_log())
        resp = self.c.create_page(body, **ctx)
        return resp["id"]

    def _attachments(self, page_id: int, pdir: str, ctx: Dict[str, Any]) -> List[str]:
        meta_path = os.path.join(pdir, ATTACH_DIR, "attachments.json")
        if not os.path.exists(long_path(meta_path)):
            return []
        errors: List[str] = []
        items = read_json(meta_path)
        existing_names = set()
        if self.opts.on_exists == "update":
            try:
                existing_names = {a.get("name") for a in self.c.attachments(page_id, **ctx)}
            except WikiApiError as exc:
                self.log.warning("attachments_list_failed", **ctx, message=str(exc), **exc.to_log())
        for a in items:
            name = a.get("name")
            local = a.get("_local_file")
            if not local:
                self.log.warning("attachment_not_in_backup", **ctx, file_name=name,
                                 message="File was not downloaded during backup, skipped")
                continue
            if name in existing_names:
                self.log.info("attachment_exists_skipped", **ctx, file_name=name)
                continue
            fpath = os.path.join(pdir, ATTACH_DIR, *local.split("/"))
            try:
                sid = self.c.upload_file(fpath, name, **ctx)
                self.c.attach_uploads(page_id, [sid], **ctx)
                self.log.info("attachment_created", **ctx, file_name=name, upload_session=sid)
            except (WikiApiError, OSError) as exc:
                self.log.exception("attachment_create_failed", exc, **ctx, step="attachment", file_name=name)
                errors.append(f"attachment {name}: {exc}")
        return errors

    def _grids(self, page_id: int, pdir: str, ctx: Dict[str, Any]):
        gdir = os.path.join(pdir, GRIDS_DIR)
        mapping: Dict[str, str] = {}
        errors: List[str] = []
        if not os.path.isdir(long_path(gdir)):
            return mapping, errors
        for fname in sorted(os.listdir(long_path(gdir))):
            if not fname.endswith(".json"):
                continue
            try:
                grid = read_json(os.path.join(gdir, fname))
            except (OSError, ValueError) as exc:
                self.log.exception("grid_read_failed", exc, **ctx, file=fname)
                errors.append(f"grid file {fname}: {exc}")
                continue
            old_id = grid.get("id")
            gctx = dict(ctx, grid_id=old_id, grid_title=grid.get("title"))
            try:
                new = self.c.create_grid(page_id, grid.get("title") or "Table", **gctx)
                new_id = new["id"]
                mapping[str(old_id)] = str(new_id)
                revision = new.get("revision")
                existing_cols = {c.get("slug") for c in ((new.get("structure") or {}).get("columns") or [])}
                columns = (grid.get("structure") or {}).get("columns") or []
                to_add = []
                for col in columns:
                    if col.get("slug") in existing_cols:
                        continue
                    to_add.append({k: v for k, v in col.items() if k != "id" and v is not None})
                if to_add:
                    r = self.c.add_columns(new_id, to_add, revision, **gctx)
                    revision = r.get("revision", revision)
                rows = grid.get("rows") or []
                payload = []
                for row in rows:
                    cells = row.get("row") or []
                    d = {}
                    for col, val in zip(columns, cells):
                        v = cell_for_api(val, col)
                        if v is not None:
                            d[col["slug"]] = v
                    payload.append(d)
                for i in range(0, len(payload), ROWS_BATCH):
                    r = self.c.add_rows(new_id, payload[i:i + ROWS_BATCH], revision, **gctx)
                    revision = (r or {}).get("revision", revision)
                self.log.info("grid_created", **gctx, new_grid_id=new_id, columns=len(to_add), rows=len(payload))
            except (WikiApiError, KeyError) as exc:
                self.log.exception("grid_create_failed", exc, **gctx, step="grid")
                errors.append(f"grid {grid.get('title')}: {exc}")
        return mapping, errors

    def _access(self, page_id: int, meta: Dict[str, Any], ctx: Dict[str, Any]) -> List[str]:
        pol = meta.get("access_policy") or {}
        atype = pol.get("access_type")
        if not atype or atype == "inherited":
            return []
        if atype == "custom":
            self.log.warning("access_custom_not_restored", **ctx, access_lists=meta.get("access_lists"),
                             message="Custom access lists must be restored manually; see @page.json access_lists")
            return []
        body = {"access_policy": {"access_type": atype}}
        if pol.get("all_staff_role"):
            body["access_policy"]["all_staff_role"] = pol["all_staff_role"]
        try:
            self.c.update_page(page_id, body, **ctx)
            self.log.info("access_restored", **ctx, access_policy=body["access_policy"])
            return []
        except WikiApiError as exc:
            self.log.exception("access_restore_failed", exc, **ctx, step="access")
            return [f"access policy: {exc}"]

    def _comments(self, page_id: int, pdir: str, ctx: Dict[str, Any]) -> List[str]:
        path = os.path.join(pdir, COMMENTS_FILE)
        if not os.path.exists(long_path(path)):
            return []
        errors: List[str] = []
        id_map: Dict[int, int] = {}
        comments = sorted(read_json(path), key=lambda c: (c.get("created_at") or "", c.get("id") or 0))
        for cm in comments:
            if cm.get("is_deleted"):
                continue
            author = (cm.get("author") or {}).get("display_name") or (cm.get("author") or {}).get("username") or "?"
            body: Dict[str, Any] = {"body": f"*{author}, {cm.get('created_at', '')}:*\n\n{cm.get('body') or ''}"}
            if cm.get("parent_id") in id_map:
                body["parent_id"] = id_map[cm["parent_id"]]
            try:
                r = self.c.create_comment(page_id, body, **ctx)
                if r.get("id") is not None:
                    id_map[cm["id"]] = r["id"]
            except WikiApiError as exc:
                self.log.exception("comment_create_failed", exc, **ctx, step="comment", comment_id=cm.get("id"))
                errors.append(f"comment {cm.get('id')}: {exc}")
        return errors

    def apply_redirects(self) -> None:
        for r in self.redirects:
            ctx = r["ctx"]
            target = ((r["redirect"] or {}).get("redirect_target") or {}).get("slug")
            if not target:
                continue
            target = target.strip("/")
            if target == self.src_root or target.startswith(self.src_root + "/"):
                target = map_slug(target, self.src_root, self.dst_root)
            try:
                self.c.update_page(r["page_id"], {"redirect": {"page": {"slug": target}}}, **ctx)
                self.log.info("redirect_restored", **ctx, redirect_to=target)
            except WikiApiError as exc:
                self.log.exception("redirect_restore_failed", exc, **ctx, step="redirect", redirect_to=target)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Re-create Yandex.Wiki pages from a Save_script backup.")
    p.add_argument("--StartPageToCreate", "--start-page", dest="start_page", required=True,
                   help="URL (https://wiki.yandex.ru/a/b/) or slug (a/b) where the saved start page is created. "
                        "Saved subpages are created below it with the same relative paths.")
    p.add_argument("--WikiBackupPages", "--backup-dir", dest="backup_dir", required=True,
                   help="Directory with the backup made by Save_script (contains manifest.json).")
    p.add_argument("--from-saved-page", default=None,
                   help="Restore only this saved page and its subpages (URL or slug as it was in the source wiki). "
                        "Default: the backup's start page.")
    p.add_argument("--on-exists", choices=["error", "skip", "update"], default="error",
                   help="What to do if a target page already exists (default: error = do not touch it)")
    p.add_argument("--wiki-base", default=None, help="Wiki web address for URLs in logs/results "
                   "(default: taken from StartPageToCreate, otherwise https://wiki.yandex.ru)")
    p.add_argument("--skip-attachments", action="store_true", help="Do not upload attached files")
    p.add_argument("--skip-grids", action="store_true", help="Do not re-create dynamic tables")
    p.add_argument("--restore-comments", action="store_true",
                   help="Also re-post saved comments (posted as you, with the original author/date in the text)")
    p.add_argument("--restore-access", action="store_true",
                   help="Also restore the page access type (inherited/all_staff). Custom lists are only logged.")
    p.add_argument("--no-rewrite-links", action="store_true",
                   help="Do not rewrite links that point into the saved tree to the new location")
    p.add_argument("--dry-run", action="store_true", help="Only show what would be created; no API calls")
    add_common_args(p)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    backup_dir = os.path.abspath(args.backup_dir)
    manifest_path = os.path.join(backup_dir, MANIFEST)
    if not os.path.exists(long_path(manifest_path)):
        print(f"ERROR: {manifest_path} not found - is this a Save_script backup directory?", file=sys.stderr)
        return 2
    manifest = read_json(manifest_path)

    stamp = run_stamp()
    logs_dir = os.path.join(backup_dir, "_logs")
    log = EventLog(os.path.join(logs_dir, f"create_log_{stamp}.jsonl"), console_level=args.log_level,
                   run_id=f"create_{stamp}")
    results = ResultsFile(os.path.join(logs_dir, f"create_results_{stamp}.txt"))

    dst_root = slug_from_input(args.start_page)
    wiki_base = args.wiki_base or wiki_base_from_input(args.start_page, manifest.get("wiki_base") or
                                                       "https://wiki.yandex.ru")
    src_root = slug_from_input(args.from_saved_page) if args.from_saved_page else manifest["root_slug"]

    client = None
    if not args.dry_run:
        auth = resolve_auth(args)
        client = WikiClient(auth["token"], auth["org_id"], auth["org_header"], args.auth_scheme, args.api_base,
                            args.timeout, args.retries, log)

    pages = [p for p in manifest.get("pages", [])
             if p.get("dir") is not None and (p["slug"] == src_root or p["slug"].startswith(src_root + "/"))]
    skipped_broken = [p for p in pages if p.get("status") != "ok" or p.get("id") is None]
    pages = [p for p in pages if p.get("id") is not None]
    pages.sort(key=lambda p: (p["slug"].count("/"), p["slug"]))

    log.info("run_start", message=f"Creating {len(pages)} page(s) from {backup_dir} at '{dst_root}'",
             backup_dir=backup_dir, source_root=src_root, target_root=dst_root, dry_run=args.dry_run,
             on_exists=args.on_exists, python=sys.version.split()[0])
    for p in skipped_broken:
        log.warning("backup_page_incomplete", slug=p["slug"], errors=p.get("errors"),
                    message="This page had errors during backup; it is restored with whatever was saved"
                    if p.get("id") is not None else "Page was not saved at all; it cannot be created")
        if p.get("id") is None:
            results.write(page_url(wiki_base, map_slug(p["slug"], src_root, dst_root)), False)

    if not pages:
        log.error("nothing_to_create", message=f"No saved pages under '{src_root}' in the manifest")
        results.close()
        log.close()
        return 2
    if pages[0]["slug"] != src_root:
        log.warning("start_page_missing_in_backup", slug=src_root,
                    message="The start page itself is not in the backup; subpages are created without it")

    creator = Creator(client, log, results, backup_dir, wiki_base, src_root, dst_root, args)
    exit_code = 0
    try:
        for i, p in enumerate(pages, 1):
            log.info("progress", message=f"{i}/{len(pages)}", slug=p["slug"])
            try:
                creator.create_page(p)
            except Exception as exc:  # never stop the whole run because of one page
                url = page_url(wiki_base, map_slug(p["slug"], src_root, dst_root))
                log.exception("page_unexpected_error", exc, page_url=url, source_slug=p["slug"])
                results.write(url, False)
        if not args.dry_run:
            creator.apply_redirects()
        exit_code = 0 if results.error == 0 else 1
    except KeyboardInterrupt:
        log.error("run_interrupted", message="Interrupted by user")
        exit_code = 130
    finally:
        log.info("run_end", message=f"ok={results.ok} error={results.error} warnings={log.counts['WARNING']}",
                 ok=results.ok, error=results.error, results_file=results.path, log_file=log.path)
        print(f"\nResults: {results.path}\nLog:     {log.path}")
        results.close()
        log.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
