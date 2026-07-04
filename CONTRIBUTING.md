# Contributing

Thanks for your interest. This is a small, single-maintainer project; issues and
focused pull requests are welcome.

## Dev setup

```bash
git clone https://github.com/Iljaadam/4ch-archive && cd 4ch-archive
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e .
4cl init          # pick a light board or two for testing
4cl start         # UI on http://127.0.0.1:8080
```

The whole app is one package, `fourchan_local/`:

| Module | Role |
|--------|------|
| `cli.py` | the `4cl` command; supervises the others |
| `poller.py` | polls the 4chan JSON API, upserts threads/posts |
| `media.py` | downloads thumbnails/files into the content-addressed store |
| `retention.py` | GC — purges 404'd, unpinned threads; honours pins |
| `db.py` | SQLite schema + write helpers |
| `app.py` | FastAPI read-only browser UI (+ pin endpoints) |
| `fourchan.py` | rate-limited API client |

Data (SQLite file + media) lives under the OS data dir; override with `FOURCHAN_DB`
and `MEDIA_STORE` for throwaway test runs.

## Guidelines

- Keep it dependency-light and cross-platform (Linux/macOS/Windows path handling).
- Match the surrounding style; explain *why* in comments where intent isn't obvious.
- Respect 4chan's API rules: the request rate stays **≤ 1 req/s**. Don't add code
  that raises it or hammers the CDN.
- **The media blocklist ships default-on for a reason** (see the README). PRs that
  weaken the default, or that add a path downloading bytes from the blocked
  photographic boards by default, will not be merged.
- Sanity-check your change end-to-end (`4cl init && 4cl start`, exercise the flow)
  before opening a PR. Note what you verified.

## Reporting a bug

Open an issue with your OS, Python version, the command you ran, and the output.
