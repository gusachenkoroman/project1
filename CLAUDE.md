# project1 — Yandex.Wiki backup tools (session resume)

Read this first when continuing work. The original task/spec is in `README.md` (do not change it without asking).
Last session: 2026-10-08/09.

## What it is
Two independent Python tools (target OS: **Windows**, Python 3.8+, only dependency `requests`) that back up
Yandex.Wiki pages and re-create them. API: https://api.wiki.yandex.net/v1 (docs: https://yandex.ru/support/wiki/ru/api-ref/).

| Dir | Entry point | Mandatory params |
|---|---|---|
| `Save_script/` | `save_script.py` | `--StartPageToSave <url or slug>` `--WikiBackupPages <dir>` |
| `Create_script/` | `create_script.py` | `--StartPageToCreate <url or slug>` `--WikiBackupPages <dir>` |
| `tests/` | `run_e2e_test.py` + `mock_wiki_server.py` | in-memory fake Wiki API; `python3 tests/run_e2e_test.py` → must print `FAILED: 0` (21 checks) |

`wiki_client.py` is **duplicated** in both tool dirs (each tool self-contained) — keep the two copies identical
(`cmp Save_script/wiki_client.py Create_script/wiki_client.py`). It holds: WikiClient (retries 429/5xx/network,
honours Retry-After), EventLog (JSONL log), ResultsFile, slug/URL helpers, Windows-safe names + `\\?\` long paths,
common CLI args (`add_common_args`, `resolve_auth`). Client method params are prefixed `p_` on purpose: callers pass
logging context as `**ctx` (keys like `slug`, `grid_id`) and plain names collided.

## Save_script behaviour
- GET start page → `/pages/descendants` (whole subtree, cursor pagination) → save pages sorted by depth.
- Per page: `@page.json` (meta incl. attributes, breadcrumbs, redirect, access_policy/access_lists/owner),
  `@content.md` (exact), `@grids/<id>.json`, `@attachments/attachments.json` + `files/`, `@comments.json`.
- Layout: `<backup>/pages/` = start page, subpages as nested folders by relative slug; `<backup>/manifest.json`.
- Page status `error` if page/grids/attachments/comments fail; access-info failures are warnings only. Never stops.

## Create_script behaviour
- Reads manifest; saved start page → `StartPageToCreate`, subpages keep relative paths; parents first.
  (Interpretation chosen: StartPageToCreate = target wiki location. `--from-saved-page` restores a sub-branch.)
- POST /pages with title/slug/content (+page_type; if API rejects it with 400/422, retried without and stops sending).
- Attachments: upload_sessions → upload_part (5 MiB) → finish → attach. Grids: create grid, add columns, add rows
  (rows sent as dicts column_slug→value; **row format is a guess, unverified on real API**), then rewrite
  `{% wgrid id="old" %}` to new ids. Links `/old/root/...` rewritten to new root (`--no-rewrite-links` to disable).
- Redirects applied at the end. Optional `--restore-comments`, `--restore-access` (custom ACLs only logged).
- `--on-exists error|skip|update` (default error), `--dry-run`.

## Logs / results (both tools)
`<WikiBackupPages>/_logs/{save|create}_log_<ts>.jsonl` (JSON Lines, everything incl. every HTTP call, token never
logged) and `{save|create}_results_<ts>.txt` (`<page URL> - status = ok|error`). Tool READMEs explain analysis
with PowerShell `ConvertFrom-Json`.

## Auth — what we learned with the user's real Wiki
- User signs in via **company SSO (federated account)** → must use an **IAM token**, not OAuth:
  `yc init --federation-id=<id>` once, then `yc iam create-token` (valid ≤ 12 h) and run with `--auth-scheme Bearer`.
- Token via `--token` or env `YANDEX_WIKI_TOKEN`; org via `--org-id` (Yandex 360, header X-Org-Id, numeric) or
  `--cloud-org-id` (Yandex Cloud, header X-Cloud-Org-Id, looks like `bpf...`). If both env vars are set, cloud wins.
- Errors seen and fixed: `403 forced_sync_required "Please authenticate user via frontend first"` (wrong auth type /
  account not synced) and `Organization collab_id=None does not exist` → **solved by the user by using the correct
  organization ID** (Cloud `organizationId`, e.g. from `yc organization-manager organization list`).

## Status
- Code committed and merged to GitHub (`gusachenkoroman`, PR #1). Tools now authenticate against the real Wiki.
- Not yet verified on the real API: full restore (Create_script), grid rows format, page_type field, comments.
- Videos: uploaded video files are attachments → saved/restored; externally embedded videos (YouTube, Disk…) keep
  only the link in `@content.md`.

## Ideas offered but not done (ask the user before doing)
- Auto-refresh IAM token by calling `yc iam create-token` from the scripts (long runs > 12 h).
- Report listing external video/embed links found in page content.
- Make `--token` a required/documented-mandatory parameter.

## Working notes
- User works on an Intel MacBook (the project folder lives there); the tools are run on Windows.
- Don't commit/push unless asked.
