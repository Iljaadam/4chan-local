"""Media worker: download thumbnails (and later full files) for boards that have
fetch_media on, into a content-addressed store deduplicated by md5.

Phases (env MEDIA_PHASE):
  thumbs  -> grab {tim}s.jpg for every file lacking a thumbnail   (default; cheap)
  full    -> grab {tim}{ext} for every file whose bytes aren't stored yet

Store layout (on-disk data dir, served read-only by the web app at /media):
  /media/thumb/<ab>/<cd>/<md5hex>.jpg
  /media/full/<ab>/<cd>/<md5hex><ext>

Dedup is automatic: the path is the md5, so a file reposted anywhere is one object.
Blocked boards (fetch_media=false) are skipped entirely — manifest only, no bytes.
"""
import os
import sys
import time

from . import db
from .fourchan import FourChan

STORE = db.default_media_store()

# Image extensions for the images-only full phase. gif counts as an image (small
# next to webm/mp4, which are the space hogs we skip).
IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".gif"]


def env(name, default):
    return os.environ.get(name, default)


def _hex(md5) -> str:
    return bytes(md5).hex()


def _path(kind: str, md5, suffix: str) -> str:
    h = _hex(md5)
    return os.path.join(STORE, kind, h[0:2], h[2:4], f"{h}{suffix}")


def file_paths(md5, ext) -> list[str]:
    """Every store path that may hold bytes for this file (thumb + full). Used by
    GC to unlink a file's on-disk objects when it becomes orphaned."""
    return [_path("thumb", md5, ".jpg"), _path("full", md5, ext or "")]


def _write_atomic(path: str, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)   # atomic on the same filesystem


def _handle_404(conn, md5, max_attempts: int):
    """404 grace: count the miss; only tombstone once attempts exhaust the window.
    Until then last_attempt_at backs the file off so we don't hammer it."""
    attempts = db.bump_fetch_attempt(conn, md5)
    if attempts >= max_attempts:
        db.mark_purged(conn, md5)           # gone past grace window, stop retrying


def _skip_on_error(conn, md5, board, tim, e):
    """A single file blew up (transient 5xx, network, disk). Back it off via its
    attempt counter so it leaves the front of the queue, and keep the batch going
    — one bad file must never wedge the whole worker."""
    conn.rollback()
    db.bump_fetch_attempt(conn, md5)
    print(f"[media] skip {board}/{tim}: {e!r}", file=sys.stderr, flush=True)


def do_thumbs(api: FourChan, conn, batch: int, backoff: int, max_attempts: int) -> int:
    rows = db.files_needing_thumb(conn, batch, backoff)
    for md5, board, tim, _ext in rows:
        path = _path("thumb", md5, ".jpg")
        if os.path.exists(path):           # already on disk, just record it
            db.mark_thumb_done(conn, md5)
            continue
        try:
            data = api.fetch_bytes(board, f"{tim}s.jpg")
            if data is None:
                _handle_404(conn, md5, max_attempts)
                continue
            _write_atomic(path, data)
            db.mark_thumb_done(conn, md5)
        except Exception as e:
            _skip_on_error(conn, md5, board, tim, e)
    return len(rows)


def do_full(api: FourChan, conn, batch: int, exts, backoff: int, max_attempts: int) -> int:
    rows = db.files_needing_full(conn, batch, exts, backoff)
    for md5, board, tim, ext in rows:
        ext = ext or ""
        path = _path("full", md5, ext)
        if os.path.exists(path):
            db.mark_full_done(conn, md5)
            continue
        try:
            data = api.fetch_bytes(board, f"{tim}{ext}")
            if data is None:
                _handle_404(conn, md5, max_attempts)
                continue
            _write_atomic(path, data)
            db.mark_full_done(conn, md5)
        except Exception as e:
            _skip_on_error(conn, md5, board, tim, e)
    return len(rows)


def main():
    db_path = env("FOURCHAN_DB", "").strip() or db.default_db_path()
    phase = env("MEDIA_PHASE", "thumbs").strip().lower()
    media_types = env("MEDIA_TYPES", "images").strip().lower()
    rps = float(env("REQ_PER_SEC_MEDIA", "1"))
    batch = int(env("MEDIA_BATCH", "200"))
    idle = int(env("MEDIA_IDLE_SLEEP", "300"))
    # 404 grace: retry a missing file up to max_attempts times, each try gated by
    # backoff seconds, before tombstoning it. Default 2 tries / 6h window: most
    # non-mod 404s are real (thread expiry), so one retry is enough to shrug off a
    # transient CDN miss without wasting fetch budget on dead files.
    backoff = int(env("MEDIA_404_BACKOFF", "21600"))
    max_attempts = int(env("MEDIA_404_MAX_ATTEMPTS", "2"))

    exts = None if media_types == "all" else IMAGE_EXTS

    conn = db.connect(db_path)
    api = FourChan(req_per_sec=rps)
    print(f"[media] phase={phase} types={media_types} rps={rps} store={STORE} "
          f"404_backoff={backoff}s max_attempts={max_attempts}", flush=True)

    while True:
        try:
            if phase == "full":
                n = do_full(api, conn, batch, exts, backoff, max_attempts)
            else:
                n = do_thumbs(api, conn, batch, backoff, max_attempts)
        except Exception as e:  # keep worker alive across transient errors
            conn.rollback()
            print(f"[media] ERROR: {e!r}", file=sys.stderr, flush=True)
            time.sleep(idle)
            continue
        if n == 0:
            time.sleep(idle)    # nothing to do; back off
        else:
            print(f"[media] processed {n} files", flush=True)


if __name__ == "__main__":
    main()
