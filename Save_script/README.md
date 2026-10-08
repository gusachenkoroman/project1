# Save_script — back up Yandex.Wiki pages

Saves a Yandex.Wiki page **and all of its subpages (children, grandchildren, …)** into a local
directory, so that `Create_script` can publish them again later.

For every page it saves everything the [Yandex.Wiki API](https://yandex.ru/support/wiki/ru/api-ref/) exposes:

| What | API used | Saved to |
|---|---|---|
| Title, type, slug, id, attributes (dates, language, keywords…), breadcrumbs, redirect | `GET /v1/pages?slug=…&fields=attributes,breadcrumbs,content,redirect` | `@page.json` |
| Page text (markup) | same call, `content` | `@content.md` (byte-exact, UTF-8) |
| Access policy, access lists, owner | `GET /v1/pages?fields=access_policy` / `access_lists` / `owner` | `@page.json` |
| Dynamic tables (grids): columns, rows, attributes | `GET /v1/pages/{id}/grids`, `GET /v1/grids/{id}` | `@grids/<grid id>.json` |
| Attached files | `GET /v1/pages/{id}/attachments`, `…/attachments/{file}/download` | `@attachments/attachments.json` + `@attachments/files/…` |
| Comments (with threads, reactions) | `GET /v1/pages/{id}/comments` | `@comments.json` |
| Subpages | `GET /v1/pages/descendants?slug=…` (all pages, cursor pagination) | one folder per subpage |

## Requirements

* Windows 10/11, Python 3.8 or newer (`py --version`)
* `py -m pip install -r requirements.txt` (installs `requests`)
* A Yandex.Wiki token for a **user** account that can read the pages
  ([how to get one](https://yandex.ru/support/wiki/ru/api-ref/access)):
  * OAuth token → `--auth-scheme OAuth` (default)
  * IAM token (federated accounts) → `--auth-scheme Bearer` (valid ≤ 12 h)
* The organization id (Yandex Tracker → Administration → Organizations):
  * Yandex 360 for Business → `--org-id` (header `X-Org-Id`)
  * Yandex Cloud / Identity Hub → `--cloud-org-id` (header `X-Cloud-Org-Id`)

## Usage

```powershell
cd Save_script
py -m pip install -r requirements.txt

# keep the token out of the command history
$env:YANDEX_WIKI_TOKEN  = "y0__xAbc..."
$env:YANDEX_WIKI_ORG_ID = "1234567"

py save_script.py --StartPageToSave "https://wiki.yandex.ru/docs/team/" --WikiBackupPages "D:\WikiBackup\2026-10-08"
```

Mandatory parameters:

| Parameter | Meaning |
|---|---|
| `--StartPageToSave` | URL (`https://wiki.yandex.ru/docs/team/`) or slug (`docs/team`) of the first page. It is saved together with its whole subtree. |
| `--WikiBackupPages` | Directory to save into (created if missing). Use a new, empty directory for every backup. |

Optional parameters: `--skip-attachments`, `--skip-grids`, `--skip-comments`, `--skip-access`,
`--deep-discovery` (also list subpages of each subpage — only needed if the API ever returns just one level),
`--token`, `--org-id`, `--cloud-org-id`, `--auth-scheme`, `--timeout`, `--retries`, `--log-level`, `--wiki-base`.
Run `py save_script.py --help` for the full list.

Exit code: `0` all pages ok, `1` at least one page failed, `2` the start page could not be read, `130` interrupted.

## How it works

1. Reads the start page (fails fast if the URL, token or org id is wrong).
2. Lists all subpages with `/pages/descendants` (paginated).
3. Saves pages **parents first** (sorted by depth). Each page is saved independently: if anything fails,
   the error is logged, the page is marked `error`, and the run **continues** with the next page.
4. Temporary errors (HTTP 429 and 5xx, network errors) are retried with back-off (`--retries`, default 5),
   honouring `Retry-After`.
5. Writes `manifest.json` (list of saved pages, their status and folders — `Create_script` reads it).

## Backup layout

```
WikiBackupPages\
  manifest.json                 list of pages, ids, statuses, folders (read by Create_script)
  pages\                        = the start page
    @page.json                  metadata (title, page_type, attributes, access, owner, redirect, …)
    @content.md                 page text exactly as returned by the API
    @comments.json
    @grids\<grid-id>.json
    @attachments\attachments.json
    @attachments\files\report.pdf
    child-page\                 = subpage  <start>/child-page
      @page.json
      @content.md
      grandchild\ …
  _logs\
    save_log_YYYYMMDD_HHMMSS.jsonl       detailed log (see below)
    save_results_YYYYMMDD_HHMMSS.txt     results file (see below)
```

Folder names are the page slugs; characters not allowed on Windows are replaced with `_`.
Long paths (> 260 characters) are supported.

## Results file

**Location:** `<WikiBackupPages>\_logs\save_results_<date>_<time>.txt` (a new file per run; the path is
also printed at the end of the run).

One line per page, written as soon as the page is finished:

```
https://wiki.yandex.ru/docs/team/ - status = ok
https://wiki.yandex.ru/docs/team/a/ - status = ok
https://wiki.yandex.ru/docs/team/b/ - status = error
```

A page is `error` if the page itself **or** any of its grids, attachments or comments could not be saved.
Missing access info (`access_lists` often needs admin rights) is only a warning and does not make a page `error`.

```powershell
# only the failed pages
Select-String -Path "D:\WikiBackup\2026-10-08\_logs\save_results_*.txt" -Pattern "status = error"
```

## Log file — how to analyse it

**Location:** `<WikiBackupPages>\_logs\save_log_<date>_<time>.jsonl`.

Format: **JSON Lines** — one JSON object per line. The file always contains everything, including every
HTTP request (`DEBUG`), regardless of `--log-level` (that only controls the console).

Fields you will use most:

| Field | Meaning |
|---|---|
| `ts`, `level` | time; `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `event` | what happened, e.g. `page_save_start`, `page_save_ok`, `page_save_error`, `page_get_failed`, `grid_save_failed`, `attachment_save_failed`, `comments_save_failed`, `discover_failed`, `http`, `http_retry` |
| `page_url`, `slug` | the page concerned |
| `step` | which part failed: `page`, `grid`, `attachment`, `comments`, `access_policy`, … |
| `http_method`, `request_url`, `http_status` | the failing API request |
| `error_code`, `message`, `error_details`, `response_body` | the API's answer (`error_code` is the field Yandex recommends to rely on) |
| `exception_type`, `traceback` | Python stack trace — points to the line in the script to change |

PowerShell examples:

```powershell
$log = Get-ChildItem "D:\WikiBackup\2026-10-08\_logs\save_log_*.jsonl" | Sort-Object LastWriteTime | Select-Object -Last 1
$rows = Get-Content $log -Encoding UTF8 | ConvertFrom-Json

# all errors, short
$rows | Where-Object level -eq "ERROR" | Format-Table ts, event, step, http_status, error_code, page_url, message -AutoSize

# errors grouped by cause - shows what to fix first
$rows | Where-Object level -eq "ERROR" | Group-Object event, http_status, error_code | Sort-Object Count -Descending | Format-Table Count, Name

# everything that happened to one page (including each HTTP request)
$rows | Where-Object page_url -eq "https://wiki.yandex.ru/docs/team/b/" | Format-List

# full details incl. the API response and the Python traceback of the first error
$rows | Where-Object level -eq "ERROR" | Select-Object -First 1 | Format-List *
```

Without PowerShell: open the `.jsonl` file in VS Code, or `findstr "\"ERROR\"" save_log_*.jsonl`.

Typical causes:

| What you see | Meaning / what to do |
|---|---|
| `http_status` 401 | token expired/invalid (IAM tokens live ≤ 12 h) — get a new one |
| `http_status` 403 | the token's user has no access to that page/file — grant access or use another account |
| `http_status` 404 on the start page | wrong URL/slug, or wrong org id / org header (`--org-id` vs `--cloud-org-id`) |
| `http_status` 429 / 5xx after retries | API overloaded — re-run later or raise `--retries` |
| `exception_type` without `http_status` | a bug or an unexpected API response — the `traceback` shows the script line |

## Notes and limits

* Page history (old revisions) is not saved — the API exposes only the current version for this purpose.
* Grid rows are saved as returned; `ticket_field` columns are computed from Yandex Tracker.
* The token is never written to the log.
* Re-running into the same directory overwrites the saved pages; logs of previous runs are kept.
