-- 4ch-archive schema, SQLite port. Local single-file tool (WAL, FTS5).
-- Timestamps are epoch INTEGER (UTC), booleans are INTEGER 0/1, md5 is BLOB.
-- Idempotent: every object uses IF NOT EXISTS so this runs safely on each boot.
-- Retention (Phase 2) purges 404'd-unpinned threads; nothing else is deleted.

CREATE TABLE IF NOT EXISTS boards (
    board       TEXT PRIMARY KEY,               -- board code, no slashes (e.g. 'g')
    title       TEXT,
    is_nsfw     INTEGER NOT NULL DEFAULT 0,
    fetch_media INTEGER NOT NULL DEFAULT 0,      -- flip per-board to grab file bytes
    enabled     INTEGER NOT NULL DEFAULT 1,
    added_at    INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);

-- Per-install key/value settings, owned by the `4cl` CLI (e.g. media phase).
-- Kept in the single DB file so there's no second config artifact to manage.
CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Content-addressed media manifest. One row per unique image (dedup by md5).
-- Populated even in text-only mode so a future media backfill has the full manifest
-- (note: file BYTES posted before media-download is enabled are unrecoverable).
CREATE TABLE IF NOT EXISTS files (
    md5        BLOB PRIMARY KEY,                 -- 4chan-supplied MD5, the dedup key
    ext        TEXT NOT NULL,                    -- '.jpg' etc
    fsize      INTEGER,
    width      INTEGER,
    height     INTEGER,
    storage    TEXT NOT NULL DEFAULT 'none',     -- full bytes: none|hot|cold|purged_before_fetch
    has_thumb  INTEGER NOT NULL DEFAULT 0,       -- thumbnail (.jpg) downloaded to the store
    refcount   INTEGER NOT NULL DEFAULT 0,       -- # posts pointing here
    first_seen INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    -- 404 grace: a missing file is retried a few times over a window before being
    -- tombstoned (storage='purged_before_fetch'), so a transient 404 isn't permanent.
    fetch_attempts  INTEGER NOT NULL DEFAULT 0,  -- failed download tries (thumb or full)
    last_attempt_at INTEGER                      -- epoch of last try; gates retry backoff
);

-- Retention pins: exempt a target from GC purge so it (and its bytes) survive
-- 4chan's 404 forever. Polymorphic on `kind`:
--   thread : (board, thread_no) — the whole thread + all its media are kept.
--   post   : (board, post_no)   — one post is kept even if its thread is purged;
--            GC keeps a thread shell alive to hold it (posts FK -> threads).
--   file   : (file_md5)         — a file's bytes are kept even when no post that
--            references its md5 survives.
-- The unused columns for a given kind stay NULL. Partial unique indexes give each
-- kind its own idempotent key without a composite NOT NULL primary key.
CREATE TABLE IF NOT EXISTS pins (
    kind      TEXT    NOT NULL CHECK (kind IN ('thread','post','file')),
    board     TEXT,
    thread_no INTEGER,
    post_no   INTEGER,
    file_md5  BLOB,
    pinned_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER))
);
CREATE UNIQUE INDEX IF NOT EXISTS pins_thread ON pins(board, thread_no) WHERE kind = 'thread';
CREATE UNIQUE INDEX IF NOT EXISTS pins_post   ON pins(board, post_no)   WHERE kind = 'post';
CREATE UNIQUE INDEX IF NOT EXISTS pins_file   ON pins(file_md5)         WHERE kind = 'file';

CREATE TABLE IF NOT EXISTS threads (
    board         TEXT    NOT NULL REFERENCES boards(board),
    thread_no     INTEGER NOT NULL,
    subject       TEXT,
    reply_count   INTEGER DEFAULT 0,
    image_count   INTEGER DEFAULT 0,
    sticky        INTEGER DEFAULT 0,
    closed        INTEGER DEFAULT 0,
    op_time       INTEGER,
    last_modified INTEGER,                        -- from threads.json; skip unchanged threads
    last_seen     INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    is_404        INTEGER NOT NULL DEFAULT 0,     -- fell off the board; archived copy kept
    archived_at   INTEGER,
    PRIMARY KEY (board, thread_no)
);

CREATE TABLE IF NOT EXISTS posts (
    board         TEXT    NOT NULL,
    post_no       INTEGER NOT NULL,
    thread_no     INTEGER NOT NULL,
    is_op         INTEGER NOT NULL DEFAULT 0,
    post_time     INTEGER,
    name          TEXT,
    trip          TEXT,
    capcode       TEXT,
    country       TEXT,
    country_name  TEXT,
    subject       TEXT,
    comment_html  TEXT,                           -- raw 'com' HTML from API
    comment_text  TEXT,                           -- stripped plaintext, for FTS + display
    -- per-post media metadata (kept even when bytes not downloaded)
    file_md5      BLOB REFERENCES files(md5),
    tim           INTEGER,                         -- 4chan file id/timestamp
    orig_filename TEXT,
    ext           TEXT,
    fsize         INTEGER,
    width         INTEGER,
    height        INTEGER,
    spoiler       INTEGER DEFAULT 0,
    deleted       INTEGER NOT NULL DEFAULT 0,
    fetched_at    INTEGER NOT NULL DEFAULT (CAST(strftime('%s','now') AS INTEGER)),
    PRIMARY KEY (board, post_no),
    FOREIGN KEY (board, thread_no) REFERENCES threads(board, thread_no)
);

CREATE INDEX IF NOT EXISTS posts_thread_idx  ON posts (board, thread_no, post_no);
CREATE INDEX IF NOT EXISTS posts_time_idx    ON posts (post_time DESC);
CREATE INDEX IF NOT EXISTS threads_board_idx ON threads (board, is_404, last_seen DESC);
-- Media worker: file -> (board, tim) resolution, OP thumbs first.
CREATE INDEX IF NOT EXISTS posts_file_idx    ON posts (file_md5, is_op) WHERE file_md5 IS NOT NULL;

-- Full-text search over comment + subject. External-content FTS5 table mirrors the
-- posts table via triggers, keyed on posts.rowid. snippet()/bm25() replace the
-- Postgres ts_headline/ts_rank path.
CREATE VIRTUAL TABLE IF NOT EXISTS posts_fts USING fts5(
    comment_text,
    subject,
    content='posts',
    content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO posts_fts(rowid, comment_text, subject)
    VALUES (new.rowid, new.comment_text, new.subject);
END;

CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, comment_text, subject)
    VALUES ('delete', old.rowid, old.comment_text, old.subject);
END;

CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO posts_fts(posts_fts, rowid, comment_text, subject)
    VALUES ('delete', old.rowid, old.comment_text, old.subject);
    INSERT INTO posts_fts(rowid, comment_text, subject)
    VALUES (new.rowid, new.comment_text, new.subject);
END;
