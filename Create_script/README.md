# Create_script — re-create Yandex.Wiki pages from a backup

Reads a backup made by `Save_script` (the `WikiBackupPages` directory) and publishes the pages to
Yandex.Wiki again through the [API](https://yandex.ru/support/wiki/ru/api-ref/).

The saved start page is created at `StartPageToCreate`; every saved subpage is created **below it with the
same relative path**, parents always before children. Example: backup of `docs/team` restored to
`restore/team` → `docs/team/a/deep` becomes `restore/team/a/deep`.

For every page it restores:

| What | API used |
|---|---|
| Page with title and text | `POST /v1/pages` (`title`, `slug`, `content`) |
| Attached files | `POST /v1/upload_sessions` → `PUT …/upload_part` (5 MB parts) → `POST …/finish` → `POST /v1/pages/{id}/attachments` |
| Dynamic tables (columns + rows) | `POST /v1/grids`, `POST /v1/grids/{id}/columns`, `POST /v1/grids/{id}/rows`; the `{% wgrid id="…" %}` macros in the text are switched to the new table ids |
| Redirects | `POST /v1/pages/{id}` with `redirect` (after all pages exist) |
| Links inside the saved tree | `/docs/team/...` links in the text are rewritten to `/restore/team/...` (turn off with `--no-rewrite-links`) |
| Comments (optional, `--restore-comments`) | `POST /v1/pages/{id}/comments` — posted as you, with original author and date in the text, threads kept |
| Access type (optional, `--restore-access`) | `POST /v1/pages/{id}` with `access_policy` (`inherited` / `all_staff`). Custom access lists are only logged — set them by hand (they are in `@page.json`). |

## Requirements

Same as Save_script: Windows, Python 3.8+, `py -m pip install -r requirements.txt`, a **user** token with
the right to create pages at the target location, and the organization id
(`--org-id` for Yandex 360, `--cloud-org-id` for Yandex Cloud; see [API access](https://yandex.ru/support/wiki/ru/api-ref/access)).

## Usage

```powershell
cd Create_script
py -m pip install -r requirements.txt
$env:YANDEX_WIKI_TOKEN  = "y0__xAbc..."
$env:YANDEX_WIKI_ORG_ID = "1234567"

# 1. see what would be created (no API calls)
py create_script.py --StartPageToCreate "https://wiki.yandex.ru/restore/team/" --WikiBackupPages "D:\WikiBackup\2026-10-08" --dry-run

# 2. create
py create_script.py --StartPageToCreate "https://wiki.yandex.ru/restore/team/" --WikiBackupPages "D:\WikiBackup\2026-10-08"
```

Mandatory parameters:

| Parameter | Meaning |
|---|---|
| `--StartPageToCreate` | URL or slug where the saved start page is created; subpages go below it. To restore into the original place, pass the original URL. |
| `--WikiBackupPages` | The backup directory (contains `manifest.json`). |

Optional parameters:

| Parameter | Meaning |
|---|---|
| `--from-saved-page URL` | restore only this saved page and its subpages (as it was named in the source wiki) |
| `--on-exists error\|skip\|update` | target page already exists: `error` (default — do not touch it, mark `error`), `skip` (leave it, mark `ok`), `update` (overwrite title and text, add missing files; tables are added again, so they may appear twice) |
| `--restore-comments`, `--restore-access` | see the table above |
| `--skip-attachments`, `--skip-grids`, `--no-rewrite-links`, `--dry-run` | |
| `--token`, `--org-id`, `--cloud-org-id`, `--auth-scheme OAuth\|Bearer`, `--timeout`, `--retries`, `--log-level`, `--wiki-base` | connection / output |

Exit code: `0` all pages ok, `1` at least one page failed, `2` nothing to create / not a backup directory, `130` interrupted.

## How it works

1. Reads `manifest.json` and selects the saved pages under the start page, sorted by depth (parents first).
2. For each page: checks whether the target exists (`--on-exists`), creates it, uploads files,
   re-creates tables, then optional access/comments. If any step fails, it is logged, the page is marked
   `error`, and the run **continues** with the next page.
3. Pages that could not be saved during the backup are listed as `error` in the results.
4. Finally sets redirects. Temporary API errors (429, 5xx, network) are retried with back-off.

If the API rejects the `page_type` field when creating a page, the script logs a warning
(`page_create_retry_without_type`) once and creates the pages without it.

## Results file

**Location:** `<WikiBackupPages>\_logs\create_results_<date>_<time>.txt` (a new file per run; the path is
also printed at the end of the run).

One line per page, with the **new** page URL:

```
https://wiki.yandex.ru/restore/team/ - status = ok
https://wiki.yandex.ru/restore/team/a/ - status = ok
https://wiki.yandex.ru/restore/team/b/ - status = error
```

`error` means the page was not created, or it was created but some part of it (a file, a table, a comment…)
failed. The log tells which.

```powershell
Select-String -Path "D:\WikiBackup\2026-10-08\_logs\create_results_*.txt" -Pattern "status = error"
```

## Log file — how to analyse it

**Location:** `<WikiBackupPages>\_logs\create_log_<date>_<time>.jsonl` (next to the Save_script logs).

Format: **JSON Lines**, one JSON object per line, always including every HTTP request (`DEBUG`).

| Field | Meaning |
|---|---|
| `ts`, `level` | time; `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `event` | e.g. `page_create_start`, `page_create_ok`, `page_create_error`, `page_create_failed`, `page_exists`, `attachment_create_failed`, `grid_create_failed`, `grid_links_update_failed`, `comment_create_failed`, `redirect_restore_failed`, `backup_read_failed`, `http`, `http_retry` |
| `page_url`, `slug` | target page; `source_slug` = the page's original slug |
| `step` | failing part: `create_page`, `attachment`, `grid`, `update_content`, `access`, `comment`, `redirect`, `read_backup` |
| `http_method`, `request_url`, `http_status`, `request_body` | the failing request (request bodies are shortened to 1000 chars) |
| `error_code`, `message`, `error_details`, `response_body` | the API's answer; `error_details` shows which field was rejected for validation errors |
| `exception_type`, `traceback` | Python stack trace — points to the line in the script to change |

```powershell
$log = Get-ChildItem "D:\WikiBackup\2026-10-08\_logs\create_log_*.jsonl" | Sort-Object LastWriteTime | Select-Object -Last 1
$rows = Get-Content $log -Encoding UTF8 | ConvertFrom-Json

# all errors
$rows | Where-Object level -eq "ERROR" | Format-Table ts, event, step, http_status, error_code, page_url, message -AutoSize

# errors grouped by cause
$rows | Where-Object level -eq "ERROR" | Group-Object event, http_status, error_code | Sort-Object Count -Descending | Format-Table Count, Name

# validation errors: which field did the API reject?
$rows | Where-Object http_status -in 400,422 | Select-Object page_url, error_code, @{n="details";e={$_.error_details | ConvertTo-Json -Compress}}, request_body

# the whole story of one page
$rows | Where-Object page_url -eq "https://wiki.yandex.ru/restore/team/a/" | Format-List
```

Typical causes:

| What you see | Meaning / what to do |
|---|---|
| `page_exists` | target already has a page — choose another `StartPageToCreate` or `--on-exists skip/update` |
| `http_status` 401 | token expired/invalid |
| `http_status` 403 | no right to create pages there |
| `http_status` 400/422 + `error_details` | the API rejected a field (e.g. slug format, a grid column option) — `error_details` names it |
| `attachment_not_in_backup` (warning) | the file failed to download during the backup; see the save log |
| `access_custom_not_restored` (warning) | the page had custom access lists; set them manually |

## Notes and limits

* Pages are created by the token's user: authors, creation dates and history are not preserved
  (the original values are in `@page.json`).
* Grid columns of type `ticket_field` are re-created, but their values come from Yandex Tracker.
* Run with `--dry-run` first, and into a test location before restoring over production.
