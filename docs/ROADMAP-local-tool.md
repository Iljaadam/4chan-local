# Roadmap — pivot to installable local-4chan tool

> **Status: COMPLETE (2026-07).** All phases below (P0 reframe → P6 package) have
> landed, plus post/file pins and a first-run setup wizard. This document is kept as
> the design record of the pivot; some details describe the plan-at-the-time (e.g.
> the datastore reader was built on sync `sqlite3`, not the `aiosqlite` floated in
> P1/P6), not necessarily the final code. See the README for current usage.

## Product

`pip install` a CLI. Pick boards. It mirrors them live to your PC, opens a local
web UI to browse + full-text search. Threads purge when 4chan 404s them — **unless
you pin them** (pins kept forever). Bounded disk: live-stock + your saved set.

```
4cl boards add g v pol      # pick boards
4cl start                   # mirror + serve UI at localhost:8080
# browse localhost:8080, click 📌 to keep a thread past its 404
```

## Design locks (decided)

| Decision        | Choice                                                        |
|-----------------|--------------------------------------------------------------|
| Datastore       | **SQLite** (single file, WAL, FTS5). Postgres = opt-in later. |
| Retention       | **Mirror + pin.** 404 → purge unless pinned.                 |
| Packaging       | **pip** + `4cl` console entry point.                         |
| Serving         | Embed static+media in the app. **Drop Docker + nginx.**      |
| Concurrency     | 1 writer (scraper), N readers (UI). SQLite WAL handles it.   |

Why SQLite is not a scaling hurdle for many boards: only ONE scraper writes, and
the 4chan API caps at 1 req/s → write rate is far below SQLite's limit. Media lives
as files on disk (bounded by mirror+pin), not in the DB. All-76-boards-all-time in
current Postgres = 6.7 GB; SQLite handles single-file DBs into the hundreds of GB.

---

## Phase 0 — Reframe (cheap, do first)

Rebrand repo from "public archive" to "local tool." No logic change.
- New name (`4chan-local` / `4cl` working title).
- Rewrite README around the install→pick→browse→pin story.
- Update `.env.example` comments; kill "public IP / OCI / hardening" framing.
- **Keep** current Docker files temporarily (fallback until Phase 5 lands).

Deliverable: repo reads as a self-host tool. Risk: none.

---

## Phase 1 — SQLite port (biggest lift)

Move off Postgres. This is the critical-path phase; everything after builds on it.

**DB access layer.** Introduce one thin module both scraper + web import, so SQL
dialect lives in ONE place:
- `scraper/db.py`: `psycopg` (sync) → `sqlite3` (stdlib, sync) — fine, scraper is
  single-threaded.
- `web/app.py`: `psycopg_pool` (async) → `aiosqlite`, or run sync `sqlite3` in a
  threadpool. Single-user local UI = low concurrency; either works. Prefer
  `aiosqlite` to keep FastAPI async.
- Placeholders `%s` → `?`. `dict_row` → `sqlite3.Row`.

**Schema port** (`db/schema.sqlite.sql`):
- `bytea` md5 → `BLOB` (unchanged logic).
- `timestamptz` → store epoch INTEGER (simplest, timezone-free math) or ISO TEXT.
  Epoch is cleaner for the 404-grace / backoff arithmetic already in `media.py`.
- `boolean` → INTEGER 0/1.
- Generated `tsv` + GIN index → **FTS5 virtual table** `posts_fts(comment_text,
  subject)` with content-sync triggers (INSERT/UPDATE/DELETE on `posts`).
- `ON CONFLICT ... DO UPDATE` upserts: SQLite supports this syntax; mostly a
  find-replace, but verify each (SQLite needs explicit conflict-target columns).

**Search port** (`web/app.py`):
- Postgres `to_tsquery` / `ts_rank` / `ts_headline` → FTS5 `MATCH`, `bm25()`,
  and `snippet()`/`highlight()`. The existing `\x02hl\x02` marker trick maps onto
  FTS5 `snippet()` start/stop markers — keep the XSS-safe escape-then-swap flow.
- Drop `statement_timeout` (Postgres-only); single-user, not needed. If wanted,
  SQLite `progress_handler` can cap runaway queries.

**Data path.** DB + media under an OS-appropriate data dir, e.g.
`~/.local/share/4chan-local/` (Linux), `%LOCALAPPDATA%` (Win), `~/Library/...`
(mac). Use `platformdirs`.

Deliverable: scraper + web run on SQLite, no Docker, against a local file. FTS works.
Risks: FTS5 trigger correctness; datetime format churn touching `media.py` backoff.
Test: fresh boot on 1 board, verify posts/threads/files land, search returns hits.

---

## Phase 2 — Retention / GC engine

Invert "never delete" → "purge on 404 unless pinned."

**Schema add:**
```sql
CREATE TABLE pins (
  board text NOT NULL, thread_no bigint NOT NULL,
  pinned_at integer NOT NULL,
  PRIMARY KEY (board, thread_no)
);
```
(Thread-level pin first; post/file-level pin = later nicety.)

**GC pass** (new `scraper/gc.py`, run each poll cycle after 404-marking):
1. Select threads where `is_404=1 AND (board,thread_no) NOT IN pins` older than a
   grace window (e.g. keep 404'd thread browsable for N hours before purge —
   configurable `PURGE_GRACE`).
2. Delete their `posts` (FTS rows follow via trigger).
3. Delete the `threads` row.
4. **Media deref:** `files.refcount` already tracks post→file. On post delete,
   decrement; when `refcount` hits 0 AND no pin references the md5, delete the
   media file(s) from the store + the `files` row.

**Pin protects transitively:** pinning a thread must bump refcount / exempt its
files from deref. Cleanest: GC's refcount recompute counts pinned threads as live.

Deliverable: disk stays bounded; 404'd-unpinned threads + orphan media disappear;
pinned threads + their media survive 404 indefinitely.
Risks: refcount drift (already a live column — audit it), accidental deletion of
shared media still referenced by a pinned thread. Add a dry-run `4cl gc --dry-run`.
Test: pin thread A, let thread B 404, run GC → B gone, A + A's media intact.

---

## Phase 3 — Pin UI

- `POST /api/pin` / `DELETE /api/pin` (board, thread_no) → writes `pins`. First
  write endpoints in a so-far read-only app; keep them localhost-only.
- 📌 button in thread + catalog + index templates (`_post.html`, `catalog.html`,
  `index.html`), toggled state from a `pins` lookup in `enrich_posts()`.
- New `/pins` view: list everything pinned (survives 404, badge it).
- Show "will purge in Xh" hint on 404'd-unpinned threads.

Deliverable: user marks keepers from the browser. Risk: low. Test: click pin,
confirm row + survives a GC pass.

---

## Phase 4 — CLI wrapper (`4cl`)

One console command orchestrates everything. `pyproject.toml` +
`[project.scripts] 4cl = "fourchan_local.cli:main"`.

```
4cl boards add g v pol      # insert into boards table (enabled=1)
4cl boards rm pol
4cl boards list
4cl start [--port 8080]     # launch scraper + media + web together
4cl stop
4cl status                  # boards, disk used, live vs pinned counts
4cl gc [--dry-run]
4cl config media thumbs|full|off   # per-install media phase
```

`start` supervises the three workers (scraper loop, media worker, uvicorn). Options:
- Simple: spawn 3 subprocesses, wait, propagate SIGINT.
- Cleaner: one asyncio process, scraper+media as tasks, uvicorn programmatic.
Recommend subprocess supervisor first (least rewrite of existing sync loops).

Deliverable: whole thing driven by `4cl`, no compose. Risk: process lifecycle /
clean shutdown on Ctrl-C. Test: `4cl start` → browse → Ctrl-C → clean exit.

---

## Phase 5 — Drop Docker + nginx

- Serve media (`/media/...`) + static from the app: FastAPI `StaticFiles` mount, or
  a small cache-header middleware. nginx microcache is irrelevant single-user.
- Delete `docker-compose*.yml`, `*/Dockerfile`, nginx conf. Keep git history.
- Media store path from the platformdirs data dir, not a Docker named volume.

Deliverable: zero-container install. Risk: losing nginx's byte-range / caching for
video scrubbing — verify `StaticFiles` serves Range requests (it does) for webm seek.

---

## Phase 6 — Package + distribute

- `pyproject.toml`: deps (`fastapi uvicorn jinja2 aiosqlite httpx platformdirs`),
  entry point, package the templates/static as package-data.
- Restructure into an importable package `fourchan_local/` (scraper, web, cli, db).
- First-run bootstrap: create data dir, init schema, friendly "pick boards" prompt.
- Cross-platform smoke test (Linux/mac/Win path handling).
- Ship: PyPI, or `pipx install 4chan-local` for an isolated CLI install.
- README quickstart: `pipx install 4chan-local && 4cl boards add g && 4cl start`.

Deliverable: `pip install` → running in 2 commands. Risk: packaging data files,
Windows path/console quirks.

---

## Sequencing

```
P0 reframe ─► P1 SQLite ─►┬─► P2 GC ─► P3 pin UI ─┐
             (critical)   └─► P4 CLI ──────────────┼─► P5 de-Docker ─► P6 package
```
P1 gates everything. P2/P4 can go in parallel after P1. P5+P6 last (they assume the
non-Docker run path exists).

## Deferred / open
- ~~Post- and file-level pins~~ — DONE: pins table is polymorphic (thread/post/file
  kinds), GC honours all three, UI has 📌/📍/💾 buttons + a grouped /pins page.
- Postgres opt-in via `--db` (keep SQL portable now, wire later).
- Blocklist default: local single-user shifts legal exposure to the user; ship
  blocklist **default-on, editable** rather than hard refusal. Decide final default
  set before public release.
- Auto-update / self-update of the CLI.
