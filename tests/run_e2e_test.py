"""
End-to-end test: Save_script -> backup dir -> Create_script, against the local mock API.

    py tests\\run_e2e_test.py
"""

import glob
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import mock_wiki_server as mws  # noqa: E402


def run(script, *args):
    cmd = [sys.executable, os.path.join(ROOT, script), *args, "--token", "t", "--org-id", "1",
           "--api-base", API, "--retries", "1", "--log-level", "WARNING"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    print(f"$ {os.path.basename(script)} -> exit {r.returncode}")
    if r.stdout.strip():
        print(r.stdout.strip())
    if r.stderr.strip():
        print(r.stderr.strip())
    return r.returncode


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        global FAILED
        FAILED += 1


FAILED = 0
srv = mws.start()
API = f"http://127.0.0.1:{srv.server_port}/v1"
s = mws.STORE

# ---- seed the "wiki"
root = s.add_page("docs/team", "Team", "Hello. See [A](/docs/team/a) and https://wiki.yandex.ru/docs/team/b/ "
                  "but not /docs/teams/x\n{% file src=\"/docs/team/.files/report.pdf\" name=\"report.pdf\" %}")
a = s.add_page("docs/team/a", "Page A", "Table:\n{% wgrid id=\"GRID_PLACEHOLDER\" %}\n")
s.add_page("docs/team/a/deep", "Deep", "deep content ✓ кириллица")
s.add_page("docs/team/b", "Page B (broken)", "x")
s.add_page("docs/other", "Not in tree", "")
s.fail_slugs.add("docs/team/b")
gid = "11111111-2222-4333-8444-555555555555"
s.grids[gid] = {"id": gid, "title": "Tasks", "page": {"id": a}, "revision": "7",
                "structure": {"columns": [{"id": "c1", "slug": "name", "title": "Name", "type": "string"},
                                          {"id": "c2", "slug": "who", "title": "Who", "type": "staff"},
                                          {"id": "c3", "slug": "done", "title": "Done", "type": "checkbox"}]},
                "rows": [{"id": "1", "row": ["first", {"username": "ivan", "identity": {"uid": "42"}}, True]},
                         {"id": "2", "row": ["second", None, False]}]}
s.pages[a]["content"] = s.pages[a]["content"].replace("GRID_PLACEHOLDER", gid)
s.blobs[1] = os.urandom(12 * 1024 * 1024 + 123)  # > 2 upload parts
s.blobs[2] = b"secret"
s.files[root] = [{"id": 1, "name": "report.pdf", "size": "x", "is_downloadable": True, "check_status": "ready"},
                 {"id": 2, "name": "locked.txt", "size": "6", "is_downloadable": True, "check_status": "ready"}]
s.fail_download.add(2)
s.comments[root] = [{"id": 1, "body": "first!", "created_at": "2026-01-01", "author": {"display_name": "Ann"}},
                    {"id": 2, "body": "reply", "parent_id": 1, "created_at": "2026-01-02",
                     "author": {"display_name": "Bob"}}]

backup = tempfile.mkdtemp(prefix="wikibackup_")

# ---- save
code = run("Save_script/save_script.py", "--StartPageToSave", "https://wiki.yandex.ru/docs/team/",
           "--WikiBackupPages", backup)
check(code == 1, "save exits 1 because some pages failed")
res = open(glob.glob(os.path.join(backup, "_logs", "save_results_*.txt"))[0], encoding="utf-8").read()
print(res)
check("https://wiki.yandex.ru/docs/team/ - status = error" in res, "root marked error (locked attachment)")
check("https://wiki.yandex.ru/docs/team/a/ - status = ok" in res, "page a ok")
check("https://wiki.yandex.ru/docs/team/a/deep/ - status = ok" in res, "grandchild saved")
check("https://wiki.yandex.ru/docs/team/b/ - status = error" in res, "broken page recorded, run continued")
check("docs/other" not in res, "pages outside the tree ignored")
check(os.path.getsize(os.path.join(backup, "pages", "@attachments", "files", "report.pdf")) == len(s.blobs[1]),
      "attachment downloaded with correct size")
check(os.path.exists(os.path.join(backup, "pages", "a", "@grids", gid + ".json")), "grid saved")
check(open(os.path.join(backup, "pages", "a", "deep", "@content.md"), encoding="utf-8").read()
      == "deep content ✓ кириллица", "unicode content saved exactly")
log_lines = [json.loads(line) for f in glob.glob(os.path.join(backup, "_logs", "save_log_*.jsonl"))
             for line in open(f, encoding="utf-8")]
errs = [r for r in log_lines if r["level"] == "ERROR"]
check(any(r.get("http_status") == 500 and r.get("slug") == "docs/team/b" for r in errs), "500 error logged with slug")
check(any(r.get("http_status") == 403 and r.get("file_name") == "locked.txt" for r in errs),
      "403 on attachment logged with file name")

# ---- create into a new location
s.fail_slugs.clear()
code = run("Create_script/create_script.py", "--StartPageToCreate", "https://wiki.yandex.ru/restore/team",
           "--WikiBackupPages", backup, "--restore-comments")
check(code == 1, "create exits 1 (page b was never saved)")
res = open(glob.glob(os.path.join(backup, "_logs", "create_results_*.txt"))[0], encoding="utf-8").read()
print(res)
new_root = s.by_slug("restore/team")
new_a = s.by_slug("restore/team/a")
check(new_root and new_a and s.by_slug("restore/team/a/deep"), "pages created at new location")
check("https://wiki.yandex.ru/restore/team/b/ - status = error" in res, "unsaved page reported as error")
check("[A](/restore/team/a)" in new_root["content"] and "wiki.yandex.ru/restore/team/b/" in new_root["content"]
      and "/docs/teams/x" in new_root["content"], "links rewritten, look-alike slug untouched")
new_grids = [g for g in s.grids.values() if g["page"]["id"] == new_a["id"]]
check(len(new_grids) == 1 and len(new_grids[0]["rows"]) == 2, "grid re-created with rows")
check(new_grids and new_grids[0]["id"] in new_a["content"] and gid not in new_a["content"],
      "wgrid macro points to new grid id")
check(new_grids and new_grids[0]["rows"][0]["row"][1] == [{"uid": "42", "username": "ivan"}],
      "staff cell converted")
up = s.files.get(new_root["id"], [])
check(len(up) == 1 and s.blobs[up[0]["id"]] == s.blobs[1], "attachment uploaded in parts, bytes identical")
check(len(s.comments.get(new_root["id"], [])) == 2 and s.comments[new_root["id"]][1]["parent_id"] is not None,
      "comments restored with thread")

# ---- second run without --on-exists must not touch existing pages
code = run("Create_script/create_script.py", "--StartPageToCreate", "restore/team", "--WikiBackupPages", backup)
check(code == 1, "re-run refuses to overwrite existing pages")
code = run("Create_script/create_script.py", "--StartPageToCreate", "restore/team", "--WikiBackupPages", backup,
           "--dry-run")
print("\nBackup dir:", backup)
print("FAILED:", FAILED)
srv.shutdown()
sys.exit(1 if FAILED else 0)
