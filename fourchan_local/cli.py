"""`4cl` — one console command that drives the whole local archive.

    4cl boards add g v pol      # enable boards (insert into boards table)
    4cl boards rm pol           # disable a board (keeps its archived data)
    4cl boards list
    4cl start [--port 8080]     # supervise scraper + media + web together
    4cl stop
    4cl status                  # boards, disk used, live vs pinned counts
    4cl gc [--dry-run]
    4cl config media thumbs|full|all|off   # per-install media phase
    4cl config poll 10                 # seconds between poll cycles (min 10)

`start` is a subprocess supervisor (roadmap Phase 4 recommendation: least rewrite
of the existing sync loops). It spawns the poller, the media worker, and uvicorn,
forwards Ctrl-C / SIGTERM to them, and exits cleanly when they're down. Boards come
from the `boards` table (enabled=1), handed to the poller via BOARDS so the poller
code is unchanged. All three children get the same FOURCHAN_DB / MEDIA_STORE so they
agree on where the single DB file and the media store live.
"""
import argparse
import os
import signal
import subprocess
import sys
import time

from fourchan_local import db, retention

# Same default as poller.DEFAULT_BLOCKLIST: photographic anon-upload boards whose
# file BYTES we never download (manifest only). Kept in sync by hand; small list.
_MEDIA_BLOCKLIST = {"b", "soc", "r", "hc", "gif", "s", "t"}

_MEDIA_PHASES = ("thumbs", "full", "all", "off")
_MIN_POLL_INTERVAL = 10
_DEFAULT_POLL_INTERVAL = 10


# ---- shared paths ----------------------------------------------------------

def _db_path() -> str:
    return os.environ.get("FOURCHAN_DB", "").strip() or db.default_db_path()


def _media_store() -> str:
    return os.environ.get("MEDIA_STORE", "").strip() or db.default_media_store()


def _pidfile(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "4cl.pid")


# ---- config table ----------------------------------------------------------

def _config_get(conn, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _config_set(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO config (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


# ---- media blocklist (persisted, handed to the poller) ---------------------
# The blocklist names boards whose file BYTES are never downloaded (text + file
# manifest are still captured). It defaults to the photographic anon-upload boards
# where illegal content — including CSAM — can be live before mods remove it; the
# media auto-fetcher would otherwise copy those bytes to the local disk unviewed.
# Stored in config so the choice sticks and is passed to the poller, which is what
# actually sets boards.fetch_media.

_BLOCKLIST_WARNING = (
    "The media worker DOWNLOADS files to THIS machine automatically, on a timer,\n"
    "before you ever open a page. On the default-blocked boards (b, soc, r, hc, gif,\n"
    "s, t) illegal content — including CSAM — is regularly live before it's removed.\n"
    "Blocklisted boards still get text + a file manifest; only the file BYTES are\n"
    "skipped. Keeping the default protects you from silently storing that content."
)


def _blocklist(conn) -> set[str]:
    raw = _config_get(conn, "media_blocklist", ",".join(sorted(_MEDIA_BLOCKLIST)))
    return {b.strip().lower() for b in raw.split(",") if b.strip()}


def _set_blocklist(conn, boards: set[str]):
    _config_set(conn, "media_blocklist", ",".join(sorted(boards)))


def _apply_blocklist(conn):
    """Re-derive every enabled board's fetch_media from the current blocklist so a
    blocklist edit shows up right away (the poller does the same on start)."""
    bl = _blocklist(conn)
    for b in _enabled_boards(conn):
        conn.execute("UPDATE boards SET fetch_media = ? WHERE board = ?",
                     (int(b not in bl), b))
    conn.commit()


def _poll_interval(conn) -> int:
    raw = _config_get(conn, "poll_interval", str(_DEFAULT_POLL_INTERVAL))
    try:
        return max(_MIN_POLL_INTERVAL, int(raw))
    except ValueError:
        return _DEFAULT_POLL_INTERVAL


# ---- boards ----------------------------------------------------------------

def _enabled_boards(conn) -> list[str]:
    cur = conn.execute("SELECT board FROM boards WHERE enabled = 1 ORDER BY board")
    return [r[0] for r in cur.fetchall()]


def cmd_boards_add(conn, boards: list[str]):
    blocklist = _blocklist(conn)
    for b in boards:
        b = b.strip().lower()
        if not b:
            continue
        db.ensure_board(conn, b, fetch_media=b not in blocklist)
        # ensure_board's upsert doesn't touch `enabled`; force it on so re-adding a
        # previously-removed board turns it back on.
        conn.execute("UPDATE boards SET enabled = 1 WHERE board = ?", (b,))
        conn.commit()
        media = "no bytes (blocklist)" if b in blocklist else "media on"
        print(f"added /{b}/  ({media})")


def cmd_boards_rm(conn, boards: list[str]):
    for b in boards:
        b = b.strip().lower()
        cur = conn.execute("UPDATE boards SET enabled = 0 WHERE board = ?", (b,))
        conn.commit()
        if cur.rowcount:
            print(f"disabled /{b}/  (archived data kept; re-add to resume)")
        else:
            print(f"/{b}/ not found", file=sys.stderr)


def cmd_boards_list(conn):
    rows = conn.execute(
        "SELECT board, enabled, fetch_media FROM boards ORDER BY board"
    ).fetchall()
    if not rows:
        print("no boards yet — `4cl boards add <board>...`")
        return
    print(f"{'board':<8} {'state':<9} media")
    for board, enabled, fetch_media in rows:
        state = "enabled" if enabled else "disabled"
        media = "bytes" if fetch_media else "manifest"
        print(f"/{board+'/':<7} {state:<9} {media}")


# ---- start / stop supervisor ----------------------------------------------

def _child_env(db_path: str, store: str) -> dict:
    env = dict(os.environ)
    env["FOURCHAN_DB"] = db_path
    env["MEDIA_STORE"] = store
    env["PYTHONUNBUFFERED"] = "1"
    return env


def cmd_start(conn, port: int):
    db_path = _db_path()
    store = _media_store()
    boards = _enabled_boards(conn)
    if not boards:
        # First-run bootstrap: an interactive terminal gets the setup wizard instead
        # of a hard error, so `pipx install ... && 4cl start` works with nothing
        # configured. Non-interactive (piped/CI) keeps erroring.
        if sys.stdin.isatty():
            cmd_init(conn)
            boards = _enabled_boards(conn)
        if not boards:
            print("no enabled boards — run `4cl init` or `4cl boards add <board>...`",
                  file=sys.stderr)
            return 1

    pidfile = _pidfile(db_path)
    if os.path.exists(pidfile):
        try:
            other = int(open(pidfile).read().strip())
            os.kill(other, 0)  # raises if not running
            print(f"already running (pid {other}); `4cl stop` first", file=sys.stderr)
            return 1
        except (ValueError, ProcessLookupError, PermissionError):
            os.remove(pidfile)  # stale pidfile

    media_phase = _config_get(conn, "media_phase", "thumbs")
    poll_interval = _poll_interval(conn)
    os.makedirs(store, exist_ok=True)  # so web's /media StaticFiles mount attaches
    env = _child_env(db_path, store)

    procs: list[subprocess.Popen] = []

    def spawn(name: str, argv: list[str], extra_env: dict | None = None):
        e = dict(env)
        if extra_env:
            e.update(extra_env)
        # Run children as package modules (`-m fourchan_local.x`) so they resolve
        # their own package data and imports regardless of cwd — no source-tree
        # layout assumptions, so a plain wheel install works.
        p = subprocess.Popen(argv, env=e)
        p._4cl_name = name  # type: ignore[attr-defined]
        procs.append(p)
        return p

    # Hand the poller the persisted blocklist explicitly (even when empty) so it
    # sets boards.fetch_media to match the user's config instead of re-applying its
    # own built-in default and clobbering it.
    spawn("scraper", [sys.executable, "-m", "fourchan_local.poller"],
          extra_env={"BOARDS": ",".join(boards),
                     "MEDIA_BLOCKLIST": ",".join(sorted(_blocklist(conn))),
                     "POLL_INTERVAL": str(poll_interval)})
    if media_phase != "off":
        media_env = {"MEDIA_PHASE": media_phase}
        if media_phase == "all":
            media_env = {"MEDIA_PHASE": "full", "MEDIA_TYPES": "all"}
        spawn("media", [sys.executable, "-m", "fourchan_local.media"],
              extra_env=media_env)
    spawn("web", [sys.executable, "-m", "uvicorn", "fourchan_local.app:app",
                  "--host", "127.0.0.1", "--port", str(port)])

    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

    print(f"[4cl] boards={boards} media={media_phase} poll={poll_interval}s "
          f"UI=http://127.0.0.1:{port}", flush=True)
    print("[4cl] Ctrl-C to stop", flush=True)

    stopping = {"flag": False}

    def shutdown(signum=None, frame=None):
        if stopping["flag"]:
            return
        stopping["flag"] = True
        print("\n[4cl] shutting down...", flush=True)
        for p in procs:
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        # Supervise: if any child dies, take the rest down too (no zombie survivors).
        while not stopping["flag"]:
            for p in procs:
                if p.poll() is not None:
                    print(f"[4cl] {p._4cl_name} exited (code {p.returncode}); "
                          f"stopping the rest", flush=True)
                    shutdown()
                    break
            time.sleep(0.5)
    finally:
        deadline = time.time() + 10
        for p in procs:
            if p.poll() is None:
                try:
                    p.wait(timeout=max(0.1, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    p.kill()
        if os.path.exists(pidfile):
            os.remove(pidfile)
        print("[4cl] stopped", flush=True)
    return 0


def cmd_stop():
    pidfile = _pidfile(_db_path())
    if not os.path.exists(pidfile):
        print("not running", file=sys.stderr)
        return 1
    try:
        pid = int(open(pidfile).read().strip())
    except ValueError:
        os.remove(pidfile)
        print("stale pidfile removed", file=sys.stderr)
        return 1
    try:
        os.kill(pid, signal.SIGTERM)
        print(f"sent SIGTERM to 4cl supervisor (pid {pid})")
    except ProcessLookupError:
        os.remove(pidfile)
        print("not running (stale pidfile removed)", file=sys.stderr)
        return 1
    return 0


# ---- status ----------------------------------------------------------------

def _dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}TB"


def cmd_status(conn):
    db_path = _db_path()
    store = _media_store()
    boards = _enabled_boards(conn)
    live = conn.execute("SELECT COUNT(*) FROM threads WHERE is_404 = 0").fetchone()[0]
    dead = conn.execute("SELECT COUNT(*) FROM threads WHERE is_404 = 1").fetchone()[0]
    pinned = conn.execute(
        "SELECT COUNT(*) FROM pins WHERE kind = 'thread'").fetchone()[0]
    posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    filecount = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    media_phase = _config_get(conn, "media_phase", "thumbs")
    poll_interval = _poll_interval(conn)

    pidfile = _pidfile(db_path)
    running = "stopped"
    if os.path.exists(pidfile):
        try:
            os.kill(int(open(pidfile).read().strip()), 0)
            running = "running"
        except (ValueError, ProcessLookupError, PermissionError):
            running = "stopped (stale pidfile)"

    db_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    media_bytes = _dir_bytes(store) if os.path.isdir(store) else 0

    print(f"status:  {running}")
    print(f"boards:  {', '.join('/'+b+'/' for b in boards) or '(none)'}")
    print(f"media:   {media_phase}")
    print(f"poll:    {poll_interval}s")
    if media_phase != "off":
        bl = _blocklist(conn)
        print(f"blocked: {' '.join(sorted(bl)) or '(none — all boards fetch bytes)'}")
    print(f"threads: {live} live, {dead} 404'd, {pinned} pinned")
    print(f"posts:   {posts}   files: {filecount}")
    print(f"disk:    db {_human(db_bytes)} + media {_human(media_bytes)} "
          f"= {_human(db_bytes + media_bytes)}")
    print(f"db:      {db_path}")
    print(f"store:   {store}")


# ---- gc / config -----------------------------------------------------------

def cmd_gc(conn, dry_run: bool):
    grace = int(os.environ.get("PURGE_GRACE", "86400"))
    stats = retention.gc_pass(conn, grace, dry_run)
    tag = "would purge" if dry_run else "purged"
    print(f"{tag}: threads={stats['threads']} posts={stats['posts']} "
          f"files={stats['files']} freed={_human(stats['bytes'])}")


def cmd_config_media(conn, phase: str):
    phase = phase.strip().lower()
    if phase not in _MEDIA_PHASES:
        print(f"media phase must be one of {'/'.join(_MEDIA_PHASES)}", file=sys.stderr)
        return 1
    _config_set(conn, "media_phase", phase)
    print(f"media phase = {phase}"
          + (" (media worker won't run)" if phase == "off" else ""))
    return 0


def cmd_config_poll(conn, seconds: int):
    if seconds < _MIN_POLL_INTERVAL:
        print(f"poll interval must be at least {_MIN_POLL_INTERVAL}s "
              "(4chan API rule)", file=sys.stderr)
        return 1
    _config_set(conn, "poll_interval", str(seconds))
    print(f"poll interval = {seconds}s")
    return 0


def _confirm_clear_blocklist(conn) -> bool:
    """Clearing the blocklist opts into downloading file bytes from EVERY enabled
    board. Gate it behind the warning + a typed confirmation so it can't happen by
    accident or from a stray argument."""
    print(_BLOCKLIST_WARNING, file=sys.stderr)
    if not sys.stdin.isatty():
        print("refusing to clear the blocklist non-interactively", file=sys.stderr)
        return False
    ans = input('\nType "I accept" to download media from ALL boards: ').strip()
    if ans == "I accept":
        _set_blocklist(conn, set())
        _apply_blocklist(conn)
        print("blocklist cleared — media will be fetched from all enabled boards")
        return True
    print("blocklist unchanged")
    return False


def cmd_config_blocklist(conn, boards: list[str] | None):
    """Show or set the media-bytes blocklist. No args = show. `none` = clear (with
    a typed confirmation). Otherwise replace with the given boards. Takes effect on
    the next `4cl start` (the poller re-applies fetch_media from it)."""
    if not boards:
        cur = _blocklist(conn)
        print(f"media blocklist (no bytes): {' '.join(sorted(cur)) or '(empty)'}")
        print("edit: `4cl config blocklist <boards...>`  |  clear: "
              "`4cl config blocklist none`")
        return 0
    if len(boards) == 1 and boards[0].strip().lower() == "none":
        _confirm_clear_blocklist(conn)
        return 0
    new = {b.strip().lower() for b in boards if b.strip()}
    _set_blocklist(conn, new)
    _apply_blocklist(conn)
    print(f"media blocklist = {' '.join(sorted(new))}")
    return 0


def cmd_init(conn) -> int:
    """First-run setup wizard: pick boards, media phase, and review the blocklist.
    Interactive only — non-TTY callers should use the individual subcommands."""
    if not sys.stdin.isatty():
        print("`4cl init` needs an interactive terminal; use `4cl boards add` / "
              "`4cl config` instead", file=sys.stderr)
        return 1

    print("fourchan-local setup — mirror 4chan boards to this machine.\n")

    print("1) Which boards to mirror? (space-separated codes, e.g. g v pol)")
    picked = input("   boards> ").split()
    if picked:
        cmd_boards_add(conn, picked)

    print("\n2) Download media?  [1] thumbnails (cheap, default)  "
          "[2] full images  [3] full media incl. videos  [4] off (text only)")
    choice = input("   media [1]> ").strip() or "1"
    phase = {"1": "thumbs", "2": "full", "3": "all", "4": "off"}.get(choice, "thumbs")
    _config_set(conn, "media_phase", phase)
    print(f"   media = {phase}")

    if phase != "off":
        print("\n3) Media-bytes blocklist (boards whose files are never downloaded).")
        print(_BLOCKLIST_WARNING)
        cur = _blocklist(conn)
        print(f"\n   current: {' '.join(sorted(cur)) or '(empty)'}")
        print("   Enter = keep default · type a new space-separated list · "
              "'none' = download everything")
        resp = input("   blocklist> ").strip()
        if resp:
            if resp.lower() == "none":
                _confirm_clear_blocklist(conn)
            else:
                new = {b.strip().lower() for b in resp.split()}
                _set_blocklist(conn, new)
                print(f"   blocklist = {' '.join(sorted(new))}")
        # Boards added earlier in this wizard kept the old fetch_media; re-apply so
        # the blocklist choice is reflected immediately.
        _apply_blocklist(conn)

    print("\nDone. Start it with:  4cl start")
    return 0


# ---- arg parsing -----------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="4cl", description="local 4chan mirror + browser")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="first-run setup wizard (boards, media, blocklist)")

    pb = sub.add_parser("boards", help="manage which boards are mirrored")
    bsub = pb.add_subparsers(dest="boards_cmd", required=True)
    pa = bsub.add_parser("add", help="enable board(s)")
    pa.add_argument("board", nargs="+")
    pr = bsub.add_parser("rm", help="disable board(s), keep archived data")
    pr.add_argument("board", nargs="+")
    bsub.add_parser("list", help="list boards and their state")

    ps = sub.add_parser("start", help="mirror + serve the UI")
    ps.add_argument("--port", type=int, default=8080)
    sub.add_parser("stop", help="stop a running mirror")
    sub.add_parser("status", help="boards, disk used, thread counts")

    pg = sub.add_parser("gc", help="purge 404'd-unpinned threads now")
    pg.add_argument("--dry-run", action="store_true")

    pc = sub.add_parser("config", help="per-install settings")
    csub = pc.add_subparsers(dest="config_cmd", required=True)
    pcm = csub.add_parser("media", help="media phase: thumbs|full|all|off")
    pcm.add_argument("phase", choices=_MEDIA_PHASES)
    pcp = csub.add_parser("poll", help="poll interval in seconds, minimum 10")
    pcp.add_argument("seconds", type=int)
    pcb = csub.add_parser("blocklist", help="show/set boards whose bytes are skipped")
    pcb.add_argument("board", nargs="*", help="new list, or 'none' to clear; empty = show")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # `start` runs its own long-lived connection; every other command opens one,
    # acts, exits. db.connect applies the schema, so first run bootstraps the file.
    conn = db.connect(_db_path())

    if args.cmd == "init":
        return cmd_init(conn) or 0
    if args.cmd == "boards":
        if args.boards_cmd == "add":
            cmd_boards_add(conn, args.board)
        elif args.boards_cmd == "rm":
            cmd_boards_rm(conn, args.board)
        else:
            cmd_boards_list(conn)
        return 0
    if args.cmd == "start":
        return cmd_start(conn, args.port) or 0
    if args.cmd == "stop":
        return cmd_stop() or 0
    if args.cmd == "status":
        cmd_status(conn)
        return 0
    if args.cmd == "gc":
        cmd_gc(conn, args.dry_run)
        return 0
    if args.cmd == "config":
        if args.config_cmd == "blocklist":
            return cmd_config_blocklist(conn, args.board) or 0
        if args.config_cmd == "poll":
            return cmd_config_poll(conn, args.seconds) or 0
        return cmd_config_media(conn, args.phase) or 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
