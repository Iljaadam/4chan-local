# fourchan-local

[![ci](https://github.com/Iljaadam/4chan-local/actions/workflows/ci.yml/badge.svg)](https://github.com/Iljaadam/4chan-local/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/fourchan-local)](https://pypi.org/project/fourchan-local/)

Run your **own local copy of 4chan** on your PC. Pick the boards you want; it mirrors
them live, gives you a local web UI to browse and full-text search, and lets you
**pin** threads to keep them forever.

The point: 4chan purges threads constantly. This mirrors what's live to your machine
and — when a thread finally 404s — throws it away **unless you pinned it**. Disk stays
bounded: whatever's currently live on your boards, plus your saved set. No unbounded
archive, no server, no account.

```
4cl init                    # pick boards, media phase, review the blocklist
4cl start                   # mirror + serve the UI
# open localhost:8080, browse, click 📌 to keep a thread past its 404
```

> ⚠️ **Legal & responsible use — read before enabling media.** This tool
> **downloads files from 4chan onto your machine automatically**. You run it, so you
> are the operator and are responsible for what lands on your disk under your local
> law. It ships with a **default-on blocklist** that skips file *bytes* from
> photographic/anonymous-upload boards (`b, soc, r, hc, gif, s, t`) where illegal
> content — including CSAM — is regularly live before moderators remove it. **Do not
> disable that blocklist** unless you fully understand the legal exposure in your
> jurisdiction. See [Content blocklist](#content-blocklist). No warranty; not
> affiliated with 4chan; respect their [API rules](https://github.com/4chan/4chan-API)
> (the poller caps at ≤1 req/s — don't raise it).

## Model

- **Mirror + pin.** Live threads are mirrored to a local DB + media store. When a
  thread 404s on 4chan it's purged locally too — **unless pinned**, in which case it
  (and its media) is kept indefinitely.
- **Bounded disk.** Steady state = live-stock of your boards + your pins. Pick a few
  boards → tens of GB that stays flat, growing only with what you pin. (For scale
  context: *all* media live across all 77 boards at any instant is ~380 GB; a typical
  2–5 board pick is ~50–100 GB.)
- **Local only.** No public surface, no accounts. UI on `localhost`.

## What runs

- **scraper** — Python, polls `a.4cdn.org` at ≤1 req/s, diffs `threads.json`,
  fetches only changed threads. Mirror+pin GC purges 404'd-unpinned threads.
- **media** — worker downloads files into a content-addressed store, deduped by md5.
- **web** — FastAPI + Jinja. Board index → catalog → thread, plus FTS search. Serves
  `/media` itself (byte-range/seek supported) — no nginx.

Single SQLite file (WAL, FTS5) + on-disk media store under your OS data dir. No
Docker, no Postgres, no nginx.

## Run (local CLI — `4cl`)

The `4cl` CLI drives the whole thing. It stores the DB + media under your OS data
dir (`~/.local/share/fourchan-local/` on Linux).

```bash
pipx install fourchan-local   # isolated CLI install; exposes the `4cl` command
4cl init                      # first-run wizard: boards, media phase, blocklist
4cl start                     # supervise scraper + media + web, UI on :8080
# browse http://127.0.0.1:8080, Ctrl-C to stop (or `4cl stop` from elsewhere)
```

`4cl init` walks you through picking boards, the media phase, and reviewing the
media-bytes **blocklist** (see below). `4cl start` on a fresh install runs the same
wizard automatically. For local hacking, `pip install -e .` from a checkout works
the same; `pip install fourchan-local` (into a venv) is the non-isolated alternative.

| Command | Does |
|---------|------|
| `4cl init` | first-run setup wizard (boards, media, blocklist) |
| `4cl boards add <b>…` | enable boards (media off for blocklisted boards) |
| `4cl boards rm <b>…` | disable a board, keeping its archived data |
| `4cl boards list` | show boards + state |
| `4cl start [--port N]` | run poller + media worker + UI together (localhost) |
| `4cl stop` | stop a running mirror |
| `4cl status` | boards, disk used, blocklist, live vs 404'd vs pinned counts |
| `4cl gc [--dry-run]` | purge 404'd-unpinned threads + orphan media now |
| `4cl config media thumbs\|full\|all\|off` | per-install media phase (`full` = images, `all` = images + videos) |
| `4cl config poll <seconds>` | seconds between poll cycles, minimum 10 |
| `4cl config blocklist [<b>… \| none]` | show/set boards whose file bytes are skipped |


## Config

Primary config is the `4cl` CLI (`init`, `boards`, `config`) — it writes the DB.
Everything else is **optional environment overrides** (see `.env.example`); nothing
auto-loads a file, so `export` them or prefix a command to use them.

| Var | Meaning |
|-----|---------|
| `FOURCHAN_DB` / `MEDIA_STORE` | override the DB file / media dir (blank = OS data dir) |
| `POLL_INTERVAL` | seconds between full poll cycles, clamped to minimum 10 |
| `REQ_PER_SEC` / `REQ_PER_SEC_MEDIA` | API / media-CDN rate caps. Keep ≤ 1 (4chan rule). |
| `PURGE_GRACE` | seconds a 404'd, unpinned thread stays before GC purges it |
| `BOARDS`, `MEDIA_PHASE`, `MEDIA_BLOCKLIST` | normally set via `4cl`; env only overrides a manual poller/media run |

The UI port is `4cl start --port N` (bound `127.0.0.1` only).

## Media store

Content-addressed, deduplicated by 4chan-supplied md5:

```
/media/thumb/<ab>/<cd>/<md5hex>.jpg     # thumbnails
/media/full/<ab>/<cd>/<md5hex><ext>     # full files (images or all-media phase)
```

The app never touches the bytes, only builds URLs from the DB. In the thread UI,
clicking an archived filename or thumbnail expands it in-place; archived `.webm` and
`.mp4` files play with native browser controls, including fullscreen. Thread pages
also have a **Media** view (`?view=media`) that keeps the OP visible and replaces
reply comments with a compact image/video grid.

### Content blocklist

The media worker **downloads file bytes to your machine automatically**, on a timer,
before you ever open a page. The blocklist names boards whose bytes are therefore
**never** downloaded (their text + file manifest are still captured). The default set
targets photographic/anonymous-upload boards (`b, soc, r, hc, gif, s, t`) where
illegal content — including CSAM — is regularly live before mods remove it; on a
**local single-user tool** that content would land on *your* disk and *your* legal
exposure.

So it ships **default-on**. `4cl init` shows it during setup; `4cl config blocklist`
edits it (`4cl config blocklist none` clears it, behind a typed confirmation). The
persisted list is handed to the poller, which sets each board's `fetch_media`. Review
it for your jurisdiction before widening media. (`MEDIA_BLOCKLIST` env still overrides
per-run for advanced/manual use.)

## Install

```bash
pipx install git+https://github.com/Iljaadam/4chan-local   # isolated CLI
# or, from a checkout, for hacking:
git clone https://github.com/Iljaadam/4chan-local && cd 4chan-local
pip install -e .
```

Python ≥ 3.10, cross-platform (data lives under your OS data dir). For the PyPI
package, use `pipx install fourchan-local`.

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and
scope. Please **do not** open PRs that weaken the media blocklist default or add a
path that downloads bytes from the blocked boards by default.

## Roadmap

Pivot plan, phased: [`docs/ROADMAP-local-tool.md`](docs/ROADMAP-local-tool.md).
Short version — P0 reframe → P1 SQLite port → P2 retention/GC → P3 pin UI →
P4 `4cl` CLI → P5 drop Docker/nginx → P6 pip package — **all done.**

## License

[MIT](LICENSE) © Ilja Adamenko. Provided **as-is, without warranty**. Not affiliated
with, endorsed by, or connected to 4chan. Using it to download content is your
responsibility under your local law.
