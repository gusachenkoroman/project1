#!/usr/bin/env python3
"""
Save_script - back up a Yandex.Wiki page and its whole subtree to a local directory.

    py save_script.py --StartPageToSave https://wiki.yandex.ru/docs/team/ --WikiBackupPages D:\\WikiBackup

See README.md for the backup layout, the log format and how to read the results file.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

from wiki_client import (EventLog, ResultsFile, WikiApiError, WikiClient, add_common_args, long_path, now_iso,
                         page_url, resolve_auth, run_stamp, safe_name, slug_from_input, wiki_base_from_input,
                         write_json, write_text)

FORMAT_VERSION = 1
CORE_FIELDS = ["attributes", "breadcrumbs", "content", "redirect"]
ACCESS_FIELDS = ["access_policy", "access_lists", "owner"]

PAGE_FILE = "@page.json"
CONTENT_FILE = "@content.md"
GRIDS_DIR = "@grids"
ATTACH_DIR = "@attachments"
COMMENTS_FILE = "@comments.json"
MANIFEST = "manifest.json"


def rel_slug(root_slug: str, slug: str) -> str:
    if slug == root_slug:
        return ""
    if root_slug and slug.startswith(root_slug + "/"):
        return slug[len(root_slug) + 1:]
    if not root_slug:
        return slug
    return slug  # should not happen; kept for safety


def page_dir(backup_dir: str, rel: str) -> str:
    parts = [safe_name(p) for p in rel.split("/") if p]
    return os.path.join(backup_dir, "pages", *parts)


class Saver:
    def __init__(self, client: WikiClient, log: EventLog, results: ResultsFile, backup_dir: str, wiki_base: str,
                 root_slug: str, opts: argparse.Namespace):
        self.c = client
        self.log = log
        self.results = results
        self.backup_dir = backup_dir
        self.wiki_base = wiki_base
        self.root_slug = root_slug
        self.opts = opts
        self.manifest_pages: List[Dict[str, Any]] = []

    # ---------------------------------------------------------------- discovery
    def discover(self, root: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Returns [{'id', 'slug'}] for the start page and every descendant (children, grandchildren, ...)."""
        found: Dict[str, Dict[str, Any]] = {root["slug"]: {"id": root["id"], "slug": root["slug"]}}
        queue = [root["slug"]]
        visited = set()
        while queue:
            slug = queue.pop(0)
            if slug in visited:
                continue
            visited.add(slug)
            url = page_url(self.wiki_base, slug)
            try:
                children = list(self.c.descendants(slug, page_url=url))
            except WikiApiError as exc:
                self.log.exception("discover_failed", exc, page_url=url, slug=slug,
                                   message=f"Could not list subpages: {exc}")
                continue
            new = 0
            for ch in children:
                s = (ch.get("slug") or "").strip("/")
                if not s or s in found:
                    continue
                found[s] = {"id": ch.get("id"), "slug": s}
                new += 1
                # /pages/descendants already returns the whole subtree; deeper calls are only needed
                # if the API ever returns just one level. --deep-discovery forces them.
                if self.opts.deep_discovery:
                    queue.append(s)
            self.log.info("discovered", page_url=url, slug=slug, message=f"{len(children)} subpages listed, {new} new",
                          count=len(children))
        pages = sorted(found.values(), key=lambda p: (p["slug"].count("/"), p["slug"]))
        return pages

    # ---------------------------------------------------------------- one page
    def save_page(self, slug: str) -> bool:
        url = page_url(self.wiki_base, slug)
        rel = rel_slug(self.root_slug, slug)
        pdir = page_dir(self.backup_dir, rel)
        ctx = {"page_url": url, "slug": slug}
        self.log.info("page_save_start", **ctx, local_dir=pdir)
        errors: List[str] = []
        warnings: List[str] = []
        entry: Dict[str, Any] = {"slug": slug, "rel_slug": rel, "dir": os.path.relpath(pdir, self.backup_dir),
                                 "url": url}

        # 1. page + content (mandatory)
        try:
            page = self.c.get_page(slug, CORE_FIELDS, **ctx)
        except WikiApiError as exc:
            self.log.exception("page_get_failed", exc, **ctx, step="page")
            entry.update(status="error", errors=[f"page: {exc}"])
            self.manifest_pages.append(entry)
            self.results.write(url, False)
            return False

        page_id = page.get("id")
        entry.update(id=page_id, title=page.get("title"), page_type=page.get("page_type"))
        if page.get("redirect"):
            self.log.warning("page_is_redirect", **ctx, message="Page has a redirect; it is saved and will be "
                             "re-created as a redirect", redirect=page.get("redirect"))

        # 2. access policy / access lists / owner (optional - often needs elevated rights)
        if not self.opts.skip_access:
            for field in ACCESS_FIELDS:
                try:
                    extra = self.c.get_page(slug, [field], **ctx)
                    page[field] = extra.get(field)
                except WikiApiError as exc:
                    warnings.append(f"{field}: {exc}")
                    self.log.warning("page_access_info_failed", **ctx, step=field, message=str(exc), **exc.to_log())

        content = page.pop("content", None)
        page["_backup"] = {"format_version": FORMAT_VERSION, "saved_at": now_iso(), "source_url": url,
                           "source_api": self.c.api_base}
        try:
            write_json(os.path.join(pdir, PAGE_FILE), page)
            write_text(os.path.join(pdir, CONTENT_FILE), content or "")
            entry["content_length"] = len(content or "")
        except OSError as exc:
            self.log.exception("page_write_failed", exc, **ctx, step="write_page")
            errors.append(f"write page: {exc}")

        if page_id is None:
            errors.append("page id missing in API response")
        else:
            # 3. dynamic tables
            if not self.opts.skip_grids:
                errors += self._save_grids(page_id, pdir, ctx, entry)
            # 4. attachments
            if not self.opts.skip_attachments:
                errors += self._save_attachments(page_id, pdir, ctx, entry)
            # 5. comments
            if not self.opts.skip_comments:
                errors += self._save_comments(page_id, pdir, ctx, entry)

        ok = not errors
        entry.update(status="ok" if ok else "error", errors=errors or None, warnings=warnings or None)
        self.manifest_pages.append(entry)
        self.results.write(url, ok)
        if ok:
            self.log.info("page_save_ok", **ctx, page_id=page_id, warnings=warnings or None,
                          message="saved" + (f" with {len(warnings)} warning(s)" if warnings else ""))
        else:
            self.log.error("page_save_error", **ctx, page_id=page_id, errors=errors,
                           message=f"{len(errors)} component(s) failed")
        return ok

    def _save_grids(self, page_id: int, pdir: str, ctx: Dict[str, Any], entry: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        gdir = os.path.join(pdir, GRIDS_DIR)
        shutil.rmtree(long_path(gdir), ignore_errors=True)
        try:
            grids = list(self.c.page_grids(page_id, **ctx))
        except WikiApiError as exc:
            self.log.exception("grids_list_failed", exc, **ctx, step="grids")
            return [f"grids list: {exc}"]
        saved = []
        for g in grids:
            gid = g.get("id")
            try:
                grid = self.c.get_grid(gid, **ctx)
                write_json(os.path.join(gdir, safe_name(str(gid)) + ".json"), grid)
                saved.append({"id": gid, "title": g.get("title"), "rows": len(grid.get("rows") or [])})
                self.log.info("grid_saved", **ctx, grid_id=gid, rows=len(grid.get("rows") or []))
            except (WikiApiError, OSError) as exc:
                self.log.exception("grid_save_failed", exc, **ctx, step="grid", grid_id=gid)
                errors.append(f"grid {gid}: {exc}")
        entry["grids"] = saved
        return errors

    def _save_attachments(self, page_id: int, pdir: str, ctx: Dict[str, Any], entry: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        adir = os.path.join(pdir, ATTACH_DIR)
        shutil.rmtree(long_path(adir), ignore_errors=True)
        try:
            items = list(self.c.attachments(page_id, **ctx))
        except WikiApiError as exc:
            self.log.exception("attachments_list_failed", exc, **ctx, step="attachments")
            return [f"attachments list: {exc}"]
        if not items:
            entry["attachments"] = 0
            return errors
        os.makedirs(long_path(os.path.join(adir, "files")), exist_ok=True)
        used_names = set()
        for a in items:
            name = a.get("name") or f"file_{a.get('id')}"
            local = safe_name(name)
            base, ext = os.path.splitext(local)
            n = 1
            while local.lower() in used_names:  # Windows is case-insensitive
                n += 1
                local = f"{base}~{n}{ext}"
            used_names.add(local.lower())
            a["_local_file"] = f"files/{local}"
            if a.get("is_downloadable") is False or a.get("check_status") in ("infected", "deleted"):
                a["_local_file"] = None
                self.log.warning("attachment_not_downloadable", **ctx, file_id=a.get("id"), file_name=name,
                                 check_status=a.get("check_status"))
                continue
            try:
                size = self.c.download_attachment(page_id, a["id"], os.path.join(adir, "files", local), **ctx)
                a["_downloaded_bytes"] = size
                self.log.info("attachment_saved", **ctx, file_id=a.get("id"), file_name=name, bytes=size)
            except (WikiApiError, OSError) as exc:
                a["_local_file"] = None
                self.log.exception("attachment_save_failed", exc, **ctx, step="attachment", file_id=a.get("id"),
                                   file_name=name)
                errors.append(f"attachment {name}: {exc}")
        write_json(os.path.join(adir, "attachments.json"), items)
        entry["attachments"] = len(items)
        return errors

    def _save_comments(self, page_id: int, pdir: str, ctx: Dict[str, Any], entry: Dict[str, Any]) -> List[str]:
        try:
            comments = list(self.c.comments(page_id, **ctx))
        except WikiApiError as exc:
            self.log.exception("comments_save_failed", exc, **ctx, step="comments")
            return [f"comments: {exc}"]
        if comments:
            write_json(os.path.join(pdir, COMMENTS_FILE), comments)
        entry["comments"] = len(comments)
        return []

    def write_manifest(self, start_input: str, finished: bool) -> None:
        write_json(os.path.join(self.backup_dir, MANIFEST), {
            "format_version": FORMAT_VERSION,
            "tool": "Save_script",
            "start_page_input": start_input,
            "root_slug": self.root_slug,
            "wiki_base": self.wiki_base,
            "api_base": self.c.api_base,
            "saved_at": now_iso(),
            "finished": finished,
            "pages": self.manifest_pages,
        })


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Back up a Yandex.Wiki page and all of its subpages.")
    p.add_argument("--StartPageToSave", "--start-page", dest="start_page", required=True,
                   help="URL (https://wiki.yandex.ru/a/b/) or slug (a/b) of the first page to save. "
                        "The page itself and all its subpages are saved.")
    p.add_argument("--WikiBackupPages", "--backup-dir", dest="backup_dir", required=True,
                   help="Directory where pages are saved (created if missing).")
    p.add_argument("--wiki-base", default=None,
                   help="Wiki web address used for page URLs in logs/results (default: taken from "
                        "StartPageToSave, otherwise https://wiki.yandex.ru)")
    p.add_argument("--skip-attachments", action="store_true", help="Do not download attached files")
    p.add_argument("--skip-grids", action="store_true", help="Do not save dynamic tables")
    p.add_argument("--skip-comments", action="store_true", help="Do not save comments")
    p.add_argument("--skip-access", action="store_true", help="Do not save access policy / access lists / owner")
    p.add_argument("--deep-discovery", action="store_true",
                   help="Also list subpages of every subpage (slower; only needed if the API returns one level)")
    add_common_args(p)
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    auth = resolve_auth(args)
    backup_dir = os.path.abspath(args.backup_dir)
    os.makedirs(long_path(backup_dir), exist_ok=True)

    stamp = run_stamp()
    logs_dir = os.path.join(backup_dir, "_logs")
    log = EventLog(os.path.join(logs_dir, f"save_log_{stamp}.jsonl"), console_level=args.log_level,
                   run_id=f"save_{stamp}")
    results = ResultsFile(os.path.join(logs_dir, f"save_results_{stamp}.txt"))
    root_slug = slug_from_input(args.start_page)
    wiki_base = args.wiki_base or wiki_base_from_input(args.start_page)
    client = WikiClient(auth["token"], auth["org_id"], auth["org_header"], args.auth_scheme, args.api_base,
                        args.timeout, args.retries, log)

    log.info("run_start", message=f"Saving '{root_slug}' into {backup_dir}", start_page=args.start_page,
             root_slug=root_slug, backup_dir=backup_dir, api_base=args.api_base, org_header=auth["org_header"],
             python=sys.version.split()[0])
    saver = Saver(client, log, results, backup_dir, wiki_base, root_slug, args)
    exit_code = 0
    try:
        start_url = page_url(wiki_base, root_slug)
        try:
            root = client.get_page(root_slug, page_url=start_url, slug=root_slug)
        except WikiApiError as exc:
            log.exception("start_page_failed", exc, page_url=start_url, slug=root_slug,
                          message=f"Start page is not accessible: {exc}")
            results.write(start_url, False)
            return 2
        root_slug = saver.root_slug = (root.get("slug") or root_slug).strip("/")
        pages = saver.discover(root)
        log.info("discovery_done", message=f"{len(pages)} page(s) to save", count=len(pages))
        for i, p in enumerate(pages, 1):
            log.info("progress", message=f"{i}/{len(pages)}", slug=p["slug"])
            try:
                saver.save_page(p["slug"])
            except Exception as exc:  # never stop the whole run because of one page
                url = page_url(wiki_base, p["slug"])
                log.exception("page_unexpected_error", exc, page_url=url, slug=p["slug"])
                saver.manifest_pages.append({"slug": p["slug"], "rel_slug": rel_slug(root_slug, p["slug"]),
                                             "url": url, "status": "error", "errors": [f"unexpected: {exc}"]})
                results.write(url, False)
            if i % 20 == 0:
                saver.write_manifest(args.start_page, finished=False)
        saver.write_manifest(args.start_page, finished=True)
        exit_code = 0 if results.error == 0 else 1
    except KeyboardInterrupt:
        log.error("run_interrupted", message="Interrupted by user")
        saver.write_manifest(args.start_page, finished=False)
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
