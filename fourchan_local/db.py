"""SQLite persistence for the scraper. Idempotent upserts; single writer.

Timestamps are epoch INTEGER (UTC). Placeholders are `?`. The DB is a single WAL
file under the platform data dir (see default_db_path); media bytes live on disk,
not here. This module is the scraper's write layer — the web app reads separately.
"""
import base64
import os
import sqlite3
import time

from .htmlstrip import strip

# Schema ships beside this module as package data (see pyproject package-data), so
# it resolves the same for an editable checkout and an installed wheel.
_SCHEMA = os.path.join(os.path.dirname(__file__), "schema.sqlite.sql")


def default_db_path() -> str:
    """OS-appropriate data dir, overridable via FOURCHAN_DB."""
    override = os.environ.get("FOURCHAN_DB", "").strip()
    if override:
        os.makedirs(os.path.dirname(os.path.abspath(override)), exist_ok=True)
        return override
    import platformdirs
    d = platformdirs.user_data_dir("fourchan-local")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "archive.db")


def default_media_store() -> str:
    override = os.environ.get("MEDIA_STORE", "").strip()
    if override:
        return override
    import platformdirs
    return os.path.join(platformdirs.user_data_dir("fourchan-local"), "media")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    # An old DB may hold the pre-polymorphic thread-only pins table, whose columns
    # the new schema's partial indexes reference. Rename it aside first so the
    # schema can create the new-shape pins, then backfill.
    legacy_pins = _rename_legacy_pins(conn)
    with open(_SCHEMA, encoding="utf-8") as f:
        conn.executescript(f.read())
    if legacy_pins:
        conn.executescript(
            """
            INSERT INTO pins (kind, board, thread_no, pinned_at)
                SELECT 'thread', board, thread_no, pinned_at FROM pins_legacy;
            DROP TABLE pins_legacy;
            """
        )
    conn.commit()
    return conn


def _rename_legacy_pins(conn) -> bool:
    """If a pins table exists in the old thread-only shape (no `kind` column),
    rename it to pins_legacy and return True so connect() backfills it into the new
    polymorphic table after the schema runs. No-op on a fresh DB or a migrated one."""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pins'"
    ).fetchone()
    if not exists:
        return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pins)")}
    if "kind" in cols:
        return False
    conn.execute("ALTER TABLE pins RENAME TO pins_legacy")
    return True


def _now() -> int:
    return int(time.time())


def _placeholders(seq) -> str:
    return ",".join("?" for _ in seq)


def ensure_board(conn, board: str, title: str | None = None, is_nsfw: bool = False,
                 fetch_media: bool = False):
    conn.execute(
        """
        INSERT INTO boards (board, title, is_nsfw, fetch_media) VALUES (?,?,?,?)
        ON CONFLICT(board) DO UPDATE SET
          title       = COALESCE(excluded.title, boards.title),
          is_nsfw     = excluded.is_nsfw,
          fetch_media = excluded.fetch_media
        """,
        (board, title, int(is_nsfw), int(fetch_media)),
    )
    conn.commit()


def get_known_last_modified(conn, board: str) -> dict[int, int]:
    """thread_no -> last_modified for live (non-404) threads we already have."""
    cur = conn.execute(
        "SELECT thread_no, last_modified FROM threads WHERE board = ? AND is_404 = 0",
        (board,),
    )
    return {no: lm for no, lm in cur.fetchall()}


def _md5_bytes(b64: str | None) -> bytes | None:
    if not b64:
        return None
    return base64.b64decode(b64)


def upsert_thread(conn, board: str, op: dict, last_modified: int):
    """Insert/update the thread row from its OP post + threads.json last_modified."""
    conn.execute(
        """
        INSERT INTO threads
          (board, thread_no, subject, reply_count, image_count,
           sticky, closed, op_time, last_modified, last_seen, is_404)
        VALUES (?,?,?,?,?,?,?,?,?,?,0)
        ON CONFLICT(board, thread_no) DO UPDATE SET
          subject       = excluded.subject,
          reply_count   = excluded.reply_count,
          image_count   = excluded.image_count,
          sticky        = excluded.sticky,
          closed        = excluded.closed,
          last_modified = excluded.last_modified,
          last_seen     = excluded.last_seen,
          is_404        = 0
        """,
        (
            board, op["no"], op.get("sub"),
            op.get("replies", 0), op.get("images", 0),
            int(bool(op.get("sticky"))), int(bool(op.get("closed"))),
            op.get("time"), last_modified, _now(),
        ),
    )


def upsert_file(conn, post: dict):
    """Record media manifest entry (dedup by md5). storage='none' until a media
    worker downloads the bytes for a board with fetch_media on."""
    md5 = _md5_bytes(post.get("md5"))
    if md5 is None:
        return None
    conn.execute(
        """
        INSERT INTO files (md5, ext, fsize, width, height, storage, refcount)
        VALUES (?,?,?,?,?,'none',0)
        ON CONFLICT(md5) DO NOTHING
        """,
        (md5, post.get("ext"), post.get("fsize"), post.get("w"), post.get("h")),
    )
    return md5


def upsert_post(conn, board: str, thread_no: int, post: dict):
    md5 = upsert_file(conn, post)
    com_html = post.get("com")
    conn.execute(
        """
        INSERT INTO posts
          (board, post_no, thread_no, is_op, post_time, name, trip, capcode,
           country, country_name, subject, comment_html, comment_text,
           file_md5, tim, orig_filename, ext, fsize, width, height, spoiler)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(board, post_no) DO UPDATE SET
          comment_html = excluded.comment_html,
          comment_text = excluded.comment_text,
          subject      = excluded.subject
        """,
        (
            board, post["no"], thread_no, int(post.get("resto", 1) == 0),
            post.get("time"), post.get("name"), post.get("trip"),
            post.get("capcode"), post.get("country"), post.get("country_name"),
            post.get("sub"), com_html, strip(com_html),
            md5, post.get("tim"),
            (post.get("filename") + post.get("ext")) if post.get("filename") else None,
            post.get("ext"), post.get("fsize"), post.get("w"), post.get("h"),
            int(bool(post.get("spoiler"))),
        ),
    )
    if md5 is not None:
        conn.execute("UPDATE files SET refcount = refcount + 1 WHERE md5 = ?", (md5,))


def save_thread(conn, board: str, posts: list[dict]):
    """Persist a full thread (OP + replies) atomically."""
    if not posts:
        return
    thread_no = posts[0]["no"]
    for p in posts:
        upsert_post(conn, board, thread_no, p)
    conn.commit()


# ---- media worker support -------------------------------------------------

def files_needing_thumb(conn, limit: int, backoff_secs: int = 0) -> list[tuple]:
    """Distinct files lacking a thumbnail, referenced by a post on a board whose
    fetch_media is on. Returns (md5, board, tim, ext) rows.

    OP thumbnails come first: a file referenced by any OP post sorts ahead of
    reply-only files, so catalogs fill in before in-thread reply thumbs.

    `backoff_secs` skips files tried recently (404 grace): a file is eligible if
    never attempted, or its last attempt is older than the backoff window."""
    cutoff = _now() - backoff_secs
    cur = conn.execute(
        """
        SELECT md5, board, tim, ext FROM (
          SELECT f.md5 AS md5, p.board AS board, p.tim AS tim, p.ext AS ext,
                 p.is_op AS is_op,
                 ROW_NUMBER() OVER (PARTITION BY f.md5 ORDER BY p.is_op DESC) AS rn
          FROM files f
          JOIN posts  p ON p.file_md5 = f.md5
          JOIN boards b ON b.board = p.board AND b.fetch_media = 1
          WHERE f.has_thumb = 0
            AND f.storage <> 'purged_before_fetch'
            AND p.tim IS NOT NULL
            AND (f.last_attempt_at IS NULL OR f.last_attempt_at < ?)
        )
        WHERE rn = 1
        ORDER BY is_op DESC, md5
        LIMIT ?
        """,
        (cutoff, limit),
    )
    return cur.fetchall()


def files_needing_full(conn, limit: int, exts: list[str] | None,
                       backoff_secs: int = 0) -> list[tuple]:
    """Distinct files whose full bytes aren't stored yet (storage='none'),
    optionally restricted to `exts` (e.g. images-only). For the images phase.
    Returns (md5, board, tim, ext, fsize) rows.

    `backoff_secs` applies the same 404 grace as files_needing_thumb."""
    cutoff = _now() - backoff_secs
    ext_clause = ""
    params: list = []
    if exts:
        ext_clause = f"AND p.ext IN ({_placeholders(exts)})"
        params.extend(exts)
    params.append(cutoff)
    params.append(limit)
    cur = conn.execute(
        f"""
        SELECT md5, board, tim, ext, fsize FROM (
          SELECT f.md5 AS md5, p.board AS board, p.tim AS tim, p.ext AS ext,
                 f.fsize AS fsize,
                 ROW_NUMBER() OVER (PARTITION BY f.md5 ORDER BY p.is_op DESC) AS rn
          FROM files f
          JOIN posts  p ON p.file_md5 = f.md5
          JOIN boards b ON b.board = p.board AND b.fetch_media = 1
          WHERE f.storage = 'none'
            AND p.tim IS NOT NULL {ext_clause}
            AND (f.last_attempt_at IS NULL OR f.last_attempt_at < ?)
        )
        WHERE rn = 1
        ORDER BY md5
        LIMIT ?
        """,
        tuple(params),
    )
    return cur.fetchall()


def mark_thumb_done(conn, md5: bytes):
    conn.execute("UPDATE files SET has_thumb = 1 WHERE md5 = ?", (md5,))
    conn.commit()


def mark_full_done(conn, md5: bytes):
    conn.execute("UPDATE files SET storage = 'hot' WHERE md5 = ?", (md5,))
    conn.commit()


def bump_fetch_attempt(conn, md5: bytes) -> int:
    """Record a failed download (404). Returns the new attempt count so the caller
    can decide whether the grace window is exhausted. last_attempt_at gates the
    retry backoff in files_needing_thumb/full."""
    cur = conn.execute(
        "UPDATE files SET fetch_attempts = fetch_attempts + 1, last_attempt_at = ? "
        "WHERE md5 = ? RETURNING fetch_attempts",
        (_now(), md5),
    )
    n = cur.fetchone()[0]
    conn.commit()
    return n


def mark_purged(conn, md5: bytes):
    """File 404'd past the grace window — bytes are gone for good. Stop retrying."""
    conn.execute(
        "UPDATE files SET storage = 'purged_before_fetch' WHERE md5 = ?", (md5,)
    )
    conn.commit()


def mark_404(conn, board: str, thread_nos: list[int]):
    if not thread_nos:
        return
    conn.execute(
        f"UPDATE threads SET is_404 = 1, archived_at = ? "
        f"WHERE board = ? AND thread_no IN ({_placeholders(thread_nos)}) AND is_404 = 0",
        (_now(), board, *thread_nos),
    )
    conn.commit()
