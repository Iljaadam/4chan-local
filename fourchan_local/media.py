"""Media worker: download thumbnails (and later full files) for boards that have
fetch_media on, into a content-addressed store deduplicated by md5.

Phases (env MEDIA_PHASE):
  thumbs  -> grab {tim}s.jpg for every file lacking a thumbnail   (default; cheap)
  full    -> grab {tim}{ext} for every image whose bytes aren't stored yet
  all     -> grab full bytes for every file type, including webm/mp4 videos

Store layout (on-disk data dir, served read-only by the web app at /media):
  /media/thumb/<ab>/<cd>/<md5hex>.jpg
  /media/full/<ab>/<cd>/<md5hex><ext>

Dedup is automatic: the path is the md5, so a file reposted anywhere is one object.
Blocked boards (fetch_media=false) are skipped entirely — manifest only, no bytes.

MEDIA_MAX_BYTES can cap the on-disk media store. 0/blank means unlimited.
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


def _parse_bytes(raw: str) -> int:
    raw = (raw or "").strip().lower()
    if not raw or raw in {"0", "off", "none", "unlimited"}:
        return 0
    units = (
        ("tb", 1024 ** 4), ("t", 1024 ** 4),
        ("gb", 1024 ** 3), ("g", 1024 ** 3),
        ("mb", 1024 ** 2), ("m", 1024 ** 2),
        ("kb", 1024), ("k", 1024),
        ("b", 1),
    )
    for suffix, multiplier in units:
        if raw.endswith(suffix):
            return int(float(raw[:-len(suffix)].strip()) * multiplier)
    return int(float(raw))


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}TB"


def _dir_bytes(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _over_cap_at(used: int, max_bytes: int, needed: int = 0) -> bool:
    if max_bytes <= 0:
        return False
    return used >= max_bytes or used + needed > max_bytes


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


def _cap_reached(max_bytes: int, used: int):
    print(f"[media] media cap reached ({_human(used)} / {_human(max_bytes)}); "
          "waiting for GC or a larger cap", flush=True)


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


def do_thumbs(api: FourChan, conn, batch: int, backoff: int, max_attempts: int,
              max_bytes: int = 0) -> int:
    rows = db.files_needing_thumb(conn, batch, backoff)
    processed = 0
    used = _dir_bytes(STORE) if max_bytes > 0 and os.path.isdir(STORE) else 0
    for md5, board, tim, _ext in rows:
        if _over_cap_at(used, max_bytes):
            _cap_reached(max_bytes, used)
            return processed
        path = _path("thumb", md5, ".jpg")
        if os.path.exists(path):           # already on disk, just record it
            db.mark_thumb_done(conn, md5)
            processed += 1
            continue
        try:
            data = api.fetch_bytes(board, f"{tim}s.jpg")
            if data is None:
                _handle_404(conn, md5, max_attempts)
                processed += 1
                continue
            if _over_cap_at(used, max_bytes, len(data)):
                _cap_reached(max_bytes, used)
                return processed
            _write_atomic(path, data)
            used += len(data)
            db.mark_thumb_done(conn, md5)
            processed += 1
        except Exception as e:
            _skip_on_error(conn, md5, board, tim, e)
            processed += 1
    return processed


def do_full(api: FourChan, conn, batch: int, exts, backoff: int, max_attempts: int,
            max_bytes: int = 0) -> int:
    rows = db.files_needing_full(conn, batch, exts, backoff)
    processed = 0
    skipped_for_cap = 0
    used = _dir_bytes(STORE) if max_bytes > 0 and os.path.isdir(STORE) else 0
    for md5, board, tim, ext, fsize in rows:
        ext = ext or ""
        if _over_cap_at(used, max_bytes):
            _cap_reached(max_bytes, used)
            return processed
        if max_bytes > 0 and fsize and _over_cap_at(used, max_bytes, int(fsize)):
            skipped_for_cap += 1
            continue
        path = _path("full", md5, ext)
        if os.path.exists(path):
            db.mark_full_done(conn, md5)
            processed += 1
            continue
        try:
            data = api.fetch_bytes(board, f"{tim}{ext}")
            if data is None:
                _handle_404(conn, md5, max_attempts)
                processed += 1
                continue
            if _over_cap_at(used, max_bytes, len(data)):
                _cap_reached(max_bytes, used)
                return processed
            _write_atomic(path, data)
            used += len(data)
            db.mark_full_done(conn, md5)
            processed += 1
        except Exception as e:
            _skip_on_error(conn, md5, board, tim, e)
            processed += 1
    if skipped_for_cap and processed == 0:
        print(f"[media] {skipped_for_cap} queued full files do not fit under "
              f"media cap ({_human(used)} / {_human(max_bytes)})", flush=True)
    return processed


def main():
    db_path = env("FOURCHAN_DB", "").strip() or db.default_db_path()
    phase = env("MEDIA_PHASE", "thumbs").strip().lower()
    media_types = env("MEDIA_TYPES", "images").strip().lower()
    if phase == "all":
        phase = "full"
        media_types = "all"
    if phase not in ("thumbs", "full"):
        print(f"[media] bad MEDIA_PHASE={phase!r}; use thumbs, full, or all",
              file=sys.stderr, flush=True)
        return 1
    rps = float(env("REQ_PER_SEC_MEDIA", "1"))
    batch = int(env("MEDIA_BATCH", "200"))
    idle = int(env("MEDIA_IDLE_SLEEP", "300"))
    try:
        max_bytes = _parse_bytes(env("MEDIA_MAX_BYTES", "0"))
    except ValueError:
        print(f"[media] bad MEDIA_MAX_BYTES={env('MEDIA_MAX_BYTES', '0')!r}; "
              "use bytes, 10GB, or off", file=sys.stderr, flush=True)
        return 1
    # 404 grace: retry a missing file up to max_attempts times, each try gated by
    # backoff seconds, before tombstoning it. Default 2 tries / 6h window: most
    # non-mod 404s are real (thread expiry), so one retry is enough to shrug off a
    # transient CDN miss without wasting fetch budget on dead files.
    backoff = int(env("MEDIA_404_BACKOFF", "21600"))
    max_attempts = int(env("MEDIA_404_MAX_ATTEMPTS", "2"))

    exts = None if media_types == "all" else IMAGE_EXTS

    conn = db.connect(db_path)
    api = FourChan(req_per_sec=rps)
    cap = _human(max_bytes) if max_bytes else "unlimited"
    print(f"[media] phase={phase} types={media_types} rps={rps} store={STORE} "
          f"cap={cap} 404_backoff={backoff}s max_attempts={max_attempts}", flush=True)

    while True:
        try:
            if phase == "full":
                n = do_thumbs(api, conn, batch, backoff, max_attempts, max_bytes)
                if n == 0:
                    n = do_full(api, conn, batch, exts, backoff, max_attempts, max_bytes)
            else:
                n = do_thumbs(api, conn, batch, backoff, max_attempts, max_bytes)
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
    sys.exit(main() or 0)
