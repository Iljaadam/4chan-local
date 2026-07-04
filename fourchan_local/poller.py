"""Poll loop: diff threads.json per board, fetch only changed threads, archive 404s.

Cycle per board:
  1. GET /{board}/threads.json -> {thread_no: last_modified}
  2. For each thread whose last_modified changed (or is new): GET full thread, upsert.
  3. Threads we knew but that vanished from threads.json -> mark is_404 (kept forever).
Then sleep POLL_INTERVAL and repeat. 4chan's API rules allow thread updates no
faster than every 10 seconds and no more than 1 request/second, so both defaults
stay at those limits.
"""
import os
import sys
import time

from . import db
from . import retention as retention_gc
from .fourchan import FourChan

# Boards whose file BYTES are never downloaded (thumbs or full): photographic,
# anonymous-upload boards where actual CSAM lands before mods remove it. We still
# capture their manifest (md5/filename/dims), just never the pixels. Drawn/hentai
# boards are adult content and are NOT blocked here (loli/shota is banned sitewide).
# Override via env MEDIA_BLOCKLIST="b,soc,...".
DEFAULT_BLOCKLIST = {"b", "soc", "r", "hc", "gif", "s", "t"}
MIN_POLL_INTERVAL = 10


def env(name, default):
    return os.environ.get(name, default)


def media_blocklist() -> set[str]:
    # Distinguish "unset" (fall back to the safe default) from "present but empty"
    # (an explicit, deliberately-empty blocklist the 4cl wizard set after warning).
    # A blank env value must NOT silently re-enable the default, or clearing the
    # blocklist would be impossible to express.
    raw = os.environ.get("MEDIA_BLOCKLIST")
    if raw is None:
        return DEFAULT_BLOCKLIST
    return {b.strip().lower() for b in raw.split(",") if b.strip()}


def main():
    db_path = env("FOURCHAN_DB", "").strip() or db.default_db_path()
    boards_cfg = env("BOARDS", "g,sci").strip()
    poll_interval = max(MIN_POLL_INTERVAL, int(env("POLL_INTERVAL", "10")))
    rps = min(float(env("REQ_PER_SEC", "1")), 1.0)
    # Retention: purge 404'd-unpinned threads archived longer than this window ago.
    # Large value ~= keep-forever; PURGE_GRACE=0 purges as soon as a thread 404s.
    purge_grace = int(env("PURGE_GRACE", "86400"))

    conn = db.connect(db_path)
    api = FourChan(req_per_sec=rps)

    blocklist = media_blocklist()
    if boards_cfg.lower() == "all":
        meta = api.boards()  # [{board, title, ws_board}]
        boards = [m["board"] for m in meta]
        for m in meta:
            db.ensure_board(conn, m["board"], m.get("title"),
                            is_nsfw=(m.get("ws_board", 1) == 0),
                            fetch_media=m["board"] not in blocklist)
        print(f"[scraper] BOARDS=all -> {len(boards)} boards", flush=True)
    else:
        boards = [b.strip() for b in boards_cfg.split(",") if b.strip()]
        for b in boards:
            db.ensure_board(conn, b, fetch_media=b not in blocklist)
    print(f"[scraper] media blocklist (no bytes): {sorted(blocklist)}", flush=True)

    print(f"[scraper] boards={boards} interval={poll_interval}s rps={rps} "
          f"purge_grace={purge_grace}s", flush=True)

    while True:
        cycle_start = time.monotonic()
        for board in boards:
            try:
                poll_board(api, conn, board)
            except Exception as e:  # keep the loop alive across transient errors
                conn.rollback()
                print(f"[scraper] ERROR board /{board}/: {e!r}", file=sys.stderr, flush=True)
        retention_gc.run(conn, purge_grace)   # purge 404'd-unpinned past grace
        elapsed = time.monotonic() - cycle_start
        sleep_for = max(0, poll_interval - elapsed)
        print(f"[scraper] cycle done in {elapsed:.1f}s, sleeping {sleep_for:.0f}s", flush=True)
        time.sleep(sleep_for)


def poll_board(api: FourChan, conn, board: str):
    live = api.threads(board)
    if live is None:
        return  # 304, board catalog unchanged
    known = db.get_known_last_modified(conn, board)
    live_nos = set()
    fetched = 0
    for t in live:
        no = t["no"]
        lm = t.get("last_modified", 0)
        live_nos.add(no)
        if known.get(no) == lm:
            continue  # unchanged, skip the expensive full-thread fetch
        posts = api.thread(board, no)
        if not posts:
            continue
        db.upsert_thread(conn, board, posts[0], lm)
        db.save_thread(conn, board, posts)
        fetched += 1

    # threads that were live before but are gone now -> 404'd, keep archived copy
    gone = [no for no in known if no not in live_nos]
    db.mark_404(conn, board, gone)
    print(f"[scraper] /{board}/ live={len(live_nos)} fetched={fetched} 404d={len(gone)}", flush=True)


if __name__ == "__main__":
    main()
