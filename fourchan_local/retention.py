"""Retention GC: purge 404'd threads (and their orphaned media) unless pinned.

Runs after each poll cycle's 404-marking (see poller.py). A thread is purgeable
when it is 404'd, not pinned, and its archived_at is older than PURGE_GRACE seconds
(so a freshly-404'd thread stays browsable for the grace window before it vanishes).

Purging a thread deletes its posts (FTS rows follow via the posts_ad trigger) and
its threads row. A file's bytes are unlinked from the store and its files-manifest
row dropped only when no surviving post references its md5.

Pins protect transitively for free: a pinned thread keeps its posts, so any md5 it
references still has a live post and is never seen as orphaned. GC decides orphaning
from the actual surviving posts, so the drift-prone files.refcount column can't
cause a wrong deletion; we recompute refcount from reality for the files we touch.
"""
import os
import sys
import time

from . import db
from . import media


def purgeable_threads(conn, cutoff: int) -> list[tuple]:
    """(board, thread_no) for 404'd threads archived before `cutoff` that carry no
    thread-level pin. archived_at IS NULL rows (age unknown) are left alone. A
    thread with only post/file pins is still purgeable — those pins protect their
    own target below, not the whole thread."""
    cur = conn.execute(
        """
        SELECT t.board, t.thread_no
        FROM threads t
        WHERE t.is_404 = 1
          AND t.archived_at IS NOT NULL
          AND t.archived_at < ?
          AND NOT EXISTS (
            SELECT 1 FROM pins p
            WHERE p.kind = 'thread' AND p.board = t.board AND p.thread_no = t.thread_no
          )
        """,
        (cutoff,),
    )
    return cur.fetchall()


def _unlink_media(md5, ext) -> int:
    """Remove a file's on-disk objects (thumb + full). Returns count removed."""
    removed = 0
    for path in media.file_paths(md5, ext):
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[gc] unlink {path}: {e!r}", file=sys.stderr, flush=True)
    return removed


def _pinned_post_nos(conn, board: str, thread_no: int) -> set:
    """post_no of any post-pinned post inside this thread."""
    cur = conn.execute(
        """
        SELECT p.post_no
        FROM posts p
        JOIN pins pn ON pn.kind = 'post' AND pn.board = p.board AND pn.post_no = p.post_no
        WHERE p.board = ? AND p.thread_no = ?
        """,
        (board, thread_no),
    )
    return {row[0] for row in cur.fetchall()}


def gc_pass(conn, grace_secs: int, dry_run: bool = False) -> dict:
    """One retention sweep. Returns stats: threads/posts/files purged, bytes freed.
    dry_run computes the same stats without deleting anything.

    Pins protect at three granularities: a thread pin keeps the whole thread (it's
    never even purgeable); a post pin keeps that one post (its thread survives as a
    shell to hold it); a file pin keeps a file's bytes regardless of posts. So a
    purgeable thread loses only its *unpinned* posts, and a file is orphaned only
    when no surviving post references it AND it isn't file-pinned."""
    now = int(time.time())
    cutoff = now - grace_secs
    threads = purgeable_threads(conn, cutoff)

    file_pinned = {row[0] for row in
                   conn.execute("SELECT file_md5 FROM pins WHERE kind = 'file'")}

    posts_to_delete: list[tuple] = []   # (board, thread_no, post_no)
    threads_emptied: list[tuple] = []   # threads with no surviving post -> row dropped
    candidates: set = set()             # md5s referenced by a to-be-deleted post
    for board, thread_no in threads:
        pinned = _pinned_post_nos(conn, board, thread_no)
        rows = conn.execute(
            "SELECT post_no, file_md5 FROM posts WHERE board = ? AND thread_no = ?",
            (board, thread_no),
        ).fetchall()
        kept_any = False
        for post_no, md5 in rows:
            if post_no in pinned:
                kept_any = True
                continue
            posts_to_delete.append((board, thread_no, post_no))
            if md5 is not None:
                candidates.add(md5)
        if not kept_any:
            threads_emptied.append((board, thread_no))

    # A candidate md5 is orphaned iff it isn't file-pinned and every post that
    # references it is in the delete set (nothing surviving keeps its bytes alive).
    delete_set = set(posts_to_delete)
    orphans: list[tuple] = []   # (md5, ext, fsize)
    for md5 in candidates:
        if md5 in file_pinned:
            continue
        refs = conn.execute(
            "SELECT board, thread_no, post_no FROM posts WHERE file_md5 = ?", (md5,)
        ).fetchall()
        if all((b, t, pn) in delete_set for b, t, pn in refs):
            meta = conn.execute(
                "SELECT ext, fsize FROM files WHERE md5 = ?", (md5,)
            ).fetchone()
            ext, fsize = (meta[0], meta[1]) if meta else (None, 0)
            orphans.append((md5, ext, fsize or 0))

    stats = {
        "threads": len(threads_emptied),
        "posts": len(posts_to_delete),
        "files": len(orphans),
        "bytes": sum(o[2] for o in orphans),
        "dry_run": dry_run,
    }
    if dry_run or not posts_to_delete:
        return stats

    # Delete unpinned posts, then drop the thread rows left with nothing (posts FK
    # -> threads, so a shell kept for a pinned post keeps its thread row).
    for board, thread_no, post_no in posts_to_delete:
        conn.execute(
            "DELETE FROM posts WHERE board = ? AND thread_no = ? AND post_no = ?",
            (board, thread_no, post_no),
        )
    for board, thread_no in threads_emptied:
        conn.execute(
            "DELETE FROM threads WHERE board = ? AND thread_no = ?", (board, thread_no)
        )

    # Deref media: orphans get unlinked + dropped; survivors get refcount corrected.
    orphan_md5s = {o[0] for o in orphans}
    for md5, ext, _fsize in orphans:
        _unlink_media(md5, ext)
        conn.execute("DELETE FROM files WHERE md5 = ?", (md5,))
    for md5 in candidates - orphan_md5s:
        n = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE file_md5 = ?", (md5,)
        ).fetchone()[0]
        conn.execute("UPDATE files SET refcount = ? WHERE md5 = ?", (n, md5))

    conn.commit()
    return stats


def run(conn, grace_secs: int, dry_run: bool = False):
    """GC one pass and log a one-line summary. Never raises past the caller loop."""
    try:
        s = gc_pass(conn, grace_secs, dry_run)
    except Exception as e:
        conn.rollback()
        print(f"[gc] ERROR: {e!r}", file=sys.stderr, flush=True)
        return
    if s["threads"] or dry_run:
        tag = "would purge" if dry_run else "purged"
        mb = s["bytes"] / 1_048_576
        print(f"[gc] {tag} threads={s['threads']} posts={s['posts']} "
              f"files={s['files']} freed={mb:.1f}MB", flush=True)


def main():
    dry_run = "--dry-run" in sys.argv[1:]
    db_path = os.environ.get("FOURCHAN_DB", "").strip() or db.default_db_path()
    grace = int(os.environ.get("PURGE_GRACE", "86400"))
    conn = db.connect(db_path)
    run(conn, grace, dry_run)


if __name__ == "__main__":
    main()
