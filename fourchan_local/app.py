"""Read-only archive frontend. Boards -> catalog -> thread, plus FTS search.

Reads the scraper's SQLite file (WAL) directly. Routes are sync `def`, so FastAPI
runs them in a threadpool; each query opens a short-lived connection in its worker
thread (WAL allows any number of concurrent readers), so no shared connection or
pool is needed for a single-user local UI."""
import html
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Query, Path, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

# Board codes are lowercase alphanumeric (e.g. g, sci, s4s, vrpg). Validating the
# path param rejects junk/traversal attempts before they ever reach a query.
BOARD_PATTERN = r"^[a-z0-9]{1,12}$"

# FTS5 snippet() markers: chosen so they can't collide with user text; we escape
# the surrounding plaintext, then swap markers for <mark> -> safe highlight, no XSS.
HL_START, HL_STOP = "\x02hl\x02", "\x03hl\x03"


def fts_query(raw: str | None) -> str:
    """Turn free user text into a safe FTS5 MATCH expression. Each word becomes a
    double-quoted string term (so FTS5 operators, columns, and syntax chars can't
    leak in or raise), joined with implicit AND. Empty -> '' (skip search)."""
    if not raw:
        return ""
    terms = re.findall(r"\w+", raw, flags=re.UNICODE)
    return " ".join('"' + t + '"' for t in terms)


def highlight(snippet: str | None) -> Markup:
    if not snippet:
        return Markup("")
    esc = html.escape(snippet)
    esc = esc.replace(html.escape(HL_START), "<mark>").replace(html.escape(HL_STOP), "</mark>")
    return Markup(esc)


# Comment renderer. Operates on already-escaped text so it is XSS-safe: the source
# is plaintext, html.escape() neutralizes any markup, then we add only our own tags.
# Plaintext extractors (for the resolver DB lookup). Note >>123 also appears inside
# >>>/b/123, so strip cross-board refs first to avoid double-counting the number.
_QUOTE_PLAIN = re.compile(r"(?<!>)>>(\d+)")            # same-board, not preceded by >
_BOARDPOST_PLAIN = re.compile(r">>>/(\w+)/(\d+)")      # cross-board post

# Resolver keys are (board, post_no) so it spans boards uniformly.
Ref = tuple[str, int]


def extract_refs(text: str | None, current_board: str) -> set[Ref]:
    """All post references in a comment, as (board, post_no): same-board >>N and
    cross-board >>>/b/N."""
    if not text:
        return set()
    refs: set[Ref] = {(current_board, int(n)) for n in _QUOTE_PLAIN.findall(text)}
    refs |= {(b, int(n)) for b, n in _BOARDPOST_PLAIN.findall(text)}
    return refs


def build_resolver(refs) -> dict[Ref, int]:
    """(board, post_no) -> thread_no, one query per distinct board."""
    by_board: dict[str, list[int]] = {}
    for b, n in refs:
        by_board.setdefault(b, []).append(n)
    out: dict[Ref, int] = {}
    for b, nos in by_board.items():
        rows = q(
            f"SELECT post_no, thread_no FROM posts "
            f"WHERE board = ? AND post_no IN ({_in_clause(nos)})",
            (b, *nos),
        )
        for r in rows:
            out[(b, r["post_no"])] = r["thread_no"]
    return out


# Single master pattern, matched once over the escaped text so replacements are never
# rescanned (sequential .sub() passes corrupt each other's output). Alternation order
# matters: a cross-board POST (>>>/b/123) must be tried before a bare board link
# (>>>/b/), which must precede a same-board quote (>>123).
_TOKEN = re.compile(
    r"(?P<url>https?://[^\s<]+)"
    r"|(?P<bpost>&gt;&gt;&gt;/(?P<bp_board>\w+)/(?P<bp_no>\d+))"
    r"|(?P<blink>&gt;&gt;&gt;/(?P<bl_board>\w+)/?)"
    r"|(?P<quote>&gt;&gt;(?P<q_no>\d+))"
)


def render_comment(text: str | None, board: str, current_thread: int,
                   resolver: dict[Ref, int]) -> Markup:
    if not text:
        return Markup("")

    def repl(m: re.Match) -> str:
        if m.group("url"):
            u = m.group("url")
            return f'<a href="{u}" target="_blank" rel="noopener nofollow ugc">{u}</a>'
        if m.group("bpost"):
            b, no = m.group("bp_board"), int(m.group("bp_no"))
            label = f"&gt;&gt;&gt;/{b}/{no}"
            tno = resolver.get((b, no))
            if tno is None:
                return f'<span class="quotelink deadlink" title="not archived">{label}</span>'
            return (f'<a class="quotelink" href="/{b}/thread/{tno}#p{no}" '
                    f'data-board="{b}" data-no="{no}">{label}</a>')
        if m.group("blink"):
            b = m.group("bl_board")
            return f'<a class="quotelink" href="/{b}">&gt;&gt;&gt;/{b}/</a>'
        # same-board quote
        no = int(m.group("q_no"))
        tno = resolver.get((board, no))
        if tno is None:
            return f'<span class="quotelink deadlink" title="not archived">&gt;&gt;{no}</span>'
        href = f"#p{no}" if tno == current_thread else f"/{board}/thread/{tno}#p{no}"
        return (f'<a class="quotelink" href="{href}" data-board="{board}" data-no="{no}">'
                f'&gt;&gt;{no}</a>')

    esc = _TOKEN.sub(repl, html.escape(text))
    out = []
    for line in esc.split("\n"):
        if line.startswith("&gt;"):
            out.append(f'<span class="quote">{line}</span>')
        else:
            out.append(line)
    return Markup("\n".join(out))

def _default_db_path() -> str:
    override = os.environ.get("FOURCHAN_DB", "").strip()
    if override:
        return override
    import platformdirs
    return os.path.join(platformdirs.user_data_dir("4chan-local"), "archive.db")


DB_PATH = _default_db_path()


def _dict_row(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}

# Interactive API docs / OpenAPI schema stay off — this is a single-user local UI,
# not an API surface, so there's nothing to document and less to fingerprint.
app = FastAPI(title="4ch-archive", docs_url=None, redoc_url=None, openapi_url=None)
# templates/ and static/ ship as package data beside this module, so they resolve
# from an installed wheel too — never depend on the process cwd.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(_PKG_DIR, "static")),
          name="static")
# App serves /media directly from the content-addressed store on disk.
MEDIA_STORE = os.environ.get("MEDIA_STORE", "").strip() or os.path.join(
    __import__("platformdirs").user_data_dir("4chan-local"), "media")
if os.path.isdir(MEDIA_STORE):
    app.mount("/media", StaticFiles(directory=MEDIA_STORE), name="media")
templates = Jinja2Templates(directory=os.path.join(_PKG_DIR, "templates"))
PAGE = 50
MAX_PAGE = 10000  # cap OFFSET so deep-paging can't force huge scans
INDEX_THREADS = 10   # threads per board-index page
INDEX_REPLIES = 3    # most-recent replies shown per thread on the index
# Grace window the GC leaves a 404'd-unpinned thread browsable before purge; must
# match retention.py's PURGE_GRACE so the "purges in Xh" hint matches reality.
PURGE_GRACE = int(os.environ.get("PURGE_GRACE", "86400"))

# Board nav appears on every page. Cache the (small, rarely-changing) list so we
# don't hit the DB per request.
_nav = {"t": 0.0, "v": []}


def nav_boards() -> list[dict]:
    """[{board, title}] for the top/bottom board lists (title -> hover tooltip)."""
    import time
    now = time.monotonic()
    if now - _nav["t"] > 300 or not _nav["v"]:
        _nav["v"] = q("SELECT board, title FROM boards WHERE enabled = 1 ORDER BY board")
        _nav["t"] = now
    return _nav["v"]


def board_title(b: str) -> str | None:
    rows = q("SELECT title FROM boards WHERE board = ?", (b,))
    return rows[0]["title"] if rows else None


templates.env.globals["nav_boards"] = nav_boards


def _hex(md5) -> str | None:
    return bytes(md5).hex() if md5 else None


def thumb_url(md5, has_thumb) -> str | None:
    if not md5 or not has_thumb:
        return None
    h = _hex(md5)
    return f"/media/thumb/{h[0:2]}/{h[2:4]}/{h}.jpg"


def full_url(md5, ext, storage) -> str | None:
    if not md5 or not ext or storage not in ("hot", "cold"):
        return None
    h = _hex(md5)
    return f"/media/full/{h[0:2]}/{h[2:4]}/{h}{ext}"


def attach_media(p: dict) -> dict:
    """Add thumb_url/full_url (and the md5 hex, for the file-pin button) to a post
    row from its file_md5 + has_thumb + storage."""
    p["thumb_url"] = thumb_url(p.get("file_md5"), p.get("has_thumb"))
    p["full_url"] = full_url(p.get("file_md5"), p.get("ext"), p.get("storage"))
    p["file_md5_hex"] = _hex(p.get("file_md5"))
    return p


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; "
        "script-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    )
    return resp


def q(sql, params=()):
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.row_factory = _dict_row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def w(sql, params=()):
    """Low-volume write path for the pin endpoints. The scraper is the primary
    writer; the UI's pin inserts/deletes are rare, so WAL's 1-writer/N-reader mix
    absorbs them. Short-lived connection, its own commit."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def pins_for(board: str, thread_nos) -> set[int]:
    """Subset of `thread_nos` on `board` that are thread-pinned. One query/page."""
    nos = list(thread_nos)
    if not nos:
        return set()
    rows = q(
        f"SELECT thread_no FROM pins WHERE kind = 'thread' AND board = ? "
        f"AND thread_no IN ({_in_clause(nos)})",
        (board, *nos),
    )
    return {r["thread_no"] for r in rows}


def post_pins_for(board: str, post_nos) -> set[int]:
    """Subset of `post_nos` on `board` that are post-pinned. One query/page."""
    nos = list(post_nos)
    if not nos:
        return set()
    rows = q(
        f"SELECT post_no FROM pins WHERE kind = 'post' AND board = ? "
        f"AND post_no IN ({_in_clause(nos)})",
        (board, *nos),
    )
    return {r["post_no"] for r in rows}


def file_pins_for(md5s) -> set[bytes]:
    """Subset of `md5s` (raw bytes) that are file-pinned. One query/page."""
    vals = [m for m in md5s if m]
    if not vals:
        return set()
    rows = q(
        f"SELECT file_md5 FROM pins WHERE kind = 'file' "
        f"AND file_md5 IN ({_in_clause(vals)})",
        tuple(vals),
    )
    return {r["file_md5"] for r in rows}


def purge_hint(is_404, archived_at, pinned) -> str | None:
    """"purges in Xh" for a 404'd, unpinned thread still inside the grace window;
    None when it's live, pinned, or its age is unknown (archived_at NULL)."""
    if not is_404 or pinned or not archived_at:
        return None
    remaining = archived_at + PURGE_GRACE - int(time.time())
    if remaining <= 0:
        return "purges any moment"
    hours = remaining / 3600
    return f"purges in {hours:.0f}h" if hours >= 1 else f"purges in {remaining // 60}m"


def fmt_time(epoch) -> str:
    """Epoch INTEGER -> display string, matching the old datetime rendering."""
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _in_clause(seq) -> str:
    return ",".join("?" for _ in seq)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Counts are derived from the small threads table, not from an exact count(*)
    # over the multi-million-row posts table (the latter scans the whole table per
    # load and gets slow as it grows). threads is exact; posts is approximate
    # (Σ reply_count+1, so it excludes deleted posts — within ~0.5%, fine here).
    boards = q(
        """
        SELECT b.board, b.title,
               coalesce(t.threads, 0) AS threads,
               coalesce(t.posts, 0)   AS posts
        FROM boards b
        LEFT JOIN (
            SELECT board, count(*) AS threads, sum(reply_count + 1) AS posts
            FROM threads GROUP BY board
        ) t ON t.board = b.board
        WHERE b.enabled = 1
        ORDER BY b.board
        """
    )
    return templates.TemplateResponse("index.html", {"request": request, "boards": boards})


# Columns selected for a rendered post (kept in sync between the thread, index and
# fragment queries). `pf` = the posts table aliased `p` joined to files `f`.
_POST_COLS = (
    "p.post_no, p.thread_no, p.is_op, p.post_time, p.name, p.trip, p.capcode, "
    "p.country, p.subject, p.comment_text, p.orig_filename, p.ext, p.fsize, "
    "p.width, p.height, p.spoiler, p.deleted, p.file_md5, p.tim, "
    "f.has_thumb, f.storage"
)


def enrich_posts(posts: list[dict], board: str, thread_no: int) -> list[dict]:
    """Resolve quotelinks, compute in-thread backlinks, render HTML and attach
    media URLs for a set of posts that all belong to one thread."""
    refs: set[Ref] = set()
    for p in posts:
        refs |= extract_refs(p["comment_text"], board)
    resolver = build_resolver(refs)
    in_thread = {p["post_no"] for p in posts}
    backlinks: dict[int, list[int]] = {}
    for p in posts:
        for n in _QUOTE_PLAIN.findall(p["comment_text"] or ""):
            n = int(n)
            if n in in_thread and n != p["post_no"]:
                backlinks.setdefault(n, []).append(p["post_no"])
    # Pin state: thread pin decorates the OP; post/file pins decorate every post
    # that carries them. One lookup each per page.
    thread_pinned = thread_no in pins_for(board, [thread_no])
    post_pinned = post_pins_for(board, [p["post_no"] for p in posts])
    file_pinned = file_pins_for(p.get("file_md5") for p in posts)
    for p in posts:
        p["backlinks"] = backlinks.get(p["post_no"], [])
        p["html"] = render_comment(p["comment_text"], board, thread_no, resolver)
        p["post_time"] = fmt_time(p.get("post_time"))
        if p["is_op"]:
            p["pinned"] = thread_pinned
        p["post_pinned"] = p["post_no"] in post_pinned
        attach_media(p)
        p["file_pinned"] = p.get("file_md5") in file_pinned
    return posts


@app.get("/pins", response_class=HTMLResponse)
def pins_view(request: Request):
    """Everything the user has pinned, newest pin first, grouped by kind (thread /
    post / file). Each is kept forever — a 404'd origin is badged so its permanence
    is clear. Declared before /{board} so the literal path wins over the wildcard."""
    threads = q(
        """
        SELECT pn.board, pn.thread_no, pn.pinned_at,
               t.subject, t.is_404, t.reply_count, t.image_count,
               op.comment_text AS op_text,
               op.file_md5, op.ext, f.has_thumb, f.storage
        FROM pins pn
        LEFT JOIN threads t ON t.board = pn.board AND t.thread_no = pn.thread_no
        LEFT JOIN posts op
               ON op.board = pn.board AND op.thread_no = pn.thread_no AND op.is_op
        LEFT JOIN files f ON f.md5 = op.file_md5
        WHERE pn.kind = 'thread'
        ORDER BY pn.pinned_at DESC
        """
    )
    for r in threads:
        attach_media(r)
        r["pinned_at_fmt"] = fmt_time(r["pinned_at"])

    posts = q(
        """
        SELECT pn.pinned_at, p.board, p.thread_no, p.post_no, p.is_op,
               p.comment_text AS op_text, t.is_404,
               p.file_md5, p.ext, f.has_thumb, f.storage
        FROM pins pn
        JOIN posts p ON p.board = pn.board AND p.post_no = pn.post_no
        LEFT JOIN threads t ON t.board = p.board AND t.thread_no = p.thread_no
        LEFT JOIN files f ON f.md5 = p.file_md5
        WHERE pn.kind = 'post'
        ORDER BY pn.pinned_at DESC
        """
    )
    for r in posts:
        attach_media(r)
        r["pinned_at_fmt"] = fmt_time(r["pinned_at"])

    files = q(
        """
        SELECT pn.pinned_at, pn.file_md5 AS file_md5, f.ext, f.fsize,
               f.has_thumb, f.storage
        FROM pins pn
        JOIN files f ON f.md5 = pn.file_md5
        WHERE pn.kind = 'file'
        ORDER BY pn.pinned_at DESC
        """
    )
    for r in files:
        attach_media(r)
        r["pinned_at_fmt"] = fmt_time(r["pinned_at"])
        # A file may be referenced from many posts; link to any one for context.
        ref = q("SELECT board, thread_no, post_no FROM posts WHERE file_md5 = ? LIMIT 1",
                (r["file_md5"],))
        r["ref"] = ref[0] if ref else None

    return templates.TemplateResponse(
        "pins.html",
        {"request": request, "threads": threads, "posts": posts, "files": files},
    )


@app.get("/{board}", response_class=HTMLResponse)
def board_index(request: Request,
                board: str = Path(pattern=BOARD_PATTERN),
                page: int = Query(1, ge=1, le=MAX_PAGE)):
    """Hayden-style index: threads as OP + the few most recent replies."""
    offset = (page - 1) * INDEX_THREADS
    threads = q(
        """
        SELECT thread_no, subject, reply_count, image_count, is_404, archived_at
        FROM threads WHERE board = ?
        ORDER BY last_seen DESC LIMIT ? OFFSET ?
        """,
        (board, INDEX_THREADS, offset),
    )
    ids = [t["thread_no"] for t in threads]
    by_thread: dict[int, list[dict]] = {}
    if ids:
        rows = q(
            f"""
            SELECT s.* FROM (
              SELECT {_POST_COLS},
                     CASE WHEN p.is_op THEN 0
                          ELSE row_number() OVER (
                               PARTITION BY p.thread_no ORDER BY p.post_no DESC) END AS rn
              FROM posts p
              LEFT JOIN files f ON f.md5 = p.file_md5
              WHERE p.board = ? AND p.thread_no IN ({_in_clause(ids)})
            ) s
            WHERE s.is_op OR s.rn <= ?
            ORDER BY s.thread_no, s.post_no
            """,
            (board, *ids, INDEX_REPLIES),
        )
        for r in rows:
            by_thread.setdefault(r["thread_no"], []).append(r)
        for tno, grp in by_thread.items():
            enrich_posts(grp, board, tno)
    for t in threads:
        grp = by_thread.get(t["thread_no"], [])
        t["op"] = grp[0] if grp else None
        t["replies"] = grp[1:]
        t["omitted"] = max((t["reply_count"] or 0) - len(t["replies"]), 0)
        if t["op"]:
            t["op"]["purge_hint"] = purge_hint(
                t["is_404"], t["archived_at"], t["op"].get("pinned"))
    return templates.TemplateResponse(
        "board.html",
        {"request": request, "board": board, "board_title": board_title(board),
         "threads": threads, "page": page},
    )


@app.get("/{board}/catalog", response_class=HTMLResponse)
def catalog(request: Request,
            board: str = Path(pattern=BOARD_PATTERN),
            page: int = Query(1, ge=1, le=MAX_PAGE)):
    """Thumbnail grid view."""
    offset = (page - 1) * PAGE
    threads = q(
        """
        SELECT t.thread_no, t.subject, t.reply_count, t.image_count,
               t.is_404, t.archived_at, t.last_seen,
               op.comment_text AS op_text,
               op.file_md5, op.ext, f.has_thumb, f.storage
        FROM threads t
        LEFT JOIN posts op
               ON op.board = t.board AND op.thread_no = t.thread_no AND op.is_op
        LEFT JOIN files f ON f.md5 = op.file_md5
        WHERE t.board = ?
        ORDER BY t.last_seen DESC
        LIMIT ? OFFSET ?
        """,
        (board, PAGE, offset),
    )
    pinned = pins_for(board, [t["thread_no"] for t in threads])
    for t in threads:
        attach_media(t)
        t["pinned"] = t["thread_no"] in pinned
        t["purge_hint"] = purge_hint(t["is_404"], t["archived_at"], t["pinned"])
    return templates.TemplateResponse(
        "catalog.html",
        {"request": request, "board": board, "board_title": board_title(board),
         "threads": threads, "page": page},
    )


@app.get("/{board}/thread/{thread_no}", response_class=HTMLResponse)
def thread(request: Request,
           board: str = Path(pattern=BOARD_PATTERN),
           thread_no: int = Path(ge=0)):
    posts = q(
        f"""
        SELECT {_POST_COLS}
        FROM posts p
        LEFT JOIN files f ON f.md5 = p.file_md5
        WHERE p.board = ? AND p.thread_no = ?
        ORDER BY p.post_no
        """,
        (board, thread_no),
    )
    meta = q(
        "SELECT subject, is_404, archived_at, reply_count "
        "FROM threads WHERE board=? AND thread_no=?",
        (board, thread_no),
    )
    enrich_posts(posts, board, thread_no)
    if meta:
        for p in posts:
            if p["is_op"]:
                p["purge_hint"] = purge_hint(
                    meta[0]["is_404"], meta[0]["archived_at"], p.get("pinned"))
    return templates.TemplateResponse(
        "thread.html",
        {"request": request, "board": board, "board_title": board_title(board),
         "thread_no": thread_no, "posts": posts, "meta": meta[0] if meta else None},
    )


@app.get("/post/{board}/{post_no}", response_class=HTMLResponse)
def post_fragment(request: Request,
                  board: str = Path(pattern=BOARD_PATTERN),
                  post_no: int = Path(ge=0)):
    """Single rendered post, for inline click-to-preview of quotelinks."""
    rows = q(
        """
        SELECT p.post_no, p.thread_no, p.is_op, p.post_time, p.name, p.trip,
               p.capcode, p.country, p.subject, p.comment_text, p.orig_filename,
               p.ext, p.fsize, p.width, p.height, p.spoiler, p.deleted,
               p.file_md5, p.tim, f.has_thumb, f.storage
        FROM posts p
        LEFT JOIN files f ON f.md5 = p.file_md5
        WHERE p.board = ? AND p.post_no = ?
        """,
        (board, post_no),
    )
    if not rows:
        return HTMLResponse(
            '<div class="post missing">post not archived</div>', status_code=404
        )
    p = rows[0]
    resolver = build_resolver(extract_refs(p["comment_text"], board))
    p["html"] = render_comment(p["comment_text"], board, p["thread_no"], resolver)
    p["backlinks"] = []
    p["post_time"] = fmt_time(p.get("post_time"))
    attach_media(p)
    return templates.TemplateResponse(
        "_post.html", {"request": request, "p": p, "board": board, "preview": True},
    )


@app.get("/search/", response_class=HTMLResponse)
def search(request: Request,
           query: str = Query("", alias="q", max_length=100),
           board: str = Query("", pattern=r"^[a-z0-9]{0,12}$"),
           page: int = Query(1, ge=1, le=MAX_PAGE)):
    results = []
    fts = fts_query(query)
    if fts:
        offset = (page - 1) * PAGE
        board_clause = "AND p.board = ?" if board else ""
        # placeholder order matches the SQL: snippet(hl-start, hl-stop) in SELECT,
        # then MATCH query, then [board], then limit, offset.
        params: list = [HL_START, HL_STOP, fts]
        if board:
            params.append(board)
        params += [PAGE, offset]
        results = q(
            f"""
            SELECT p.board, p.post_no, p.thread_no, p.post_time, p.subject,
                   snippet(posts_fts, 0, ?, ?, ' … ', 20) AS snippet
            FROM posts_fts
            JOIN posts p ON p.rowid = posts_fts.rowid
            WHERE posts_fts MATCH ? {board_clause}
            ORDER BY p.post_time DESC
            LIMIT ? OFFSET ?
            """,
            tuple(params),
        )
        for r in results:
            r["snippet"] = highlight(r["snippet"])
            r["post_time"] = fmt_time(r.get("post_time"))
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "query": query, "board": board,
         "results": results, "page": page},
    )


# ---- pins (the only write endpoints) --------------------------------------
# Pinning exempts a thread from retention GC (retention.py): a pinned thread and
# its media survive 4chan's 404 forever. These are the sole mutations in an
# otherwise read-only app, so they are gated to loopback clients only.

_LOCAL_HOSTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


class PinReq(BaseModel):
    kind: str = "thread"               # thread | post | file
    board: str | None = None
    thread_no: int | None = None
    post_no: int | None = None
    file_md5: str | None = None        # hex


def _require_local(request: Request):
    host = request.client.host if request.client else ""
    if host not in _LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="pins are local-only")


def _pin_target(req: PinReq) -> tuple[str, tuple]:
    """Validate a PinReq and return (where_sql, params) selecting exactly one pin
    row for the given kind. Same predicate is used to insert, delete, and dedup."""
    if req.kind == "thread":
        if not req.board or not re.match(BOARD_PATTERN, req.board) \
                or req.thread_no is None or req.thread_no < 0:
            raise HTTPException(status_code=400, detail="bad board or thread_no")
        return ("kind = 'thread' AND board = ? AND thread_no = ?",
                (req.board, req.thread_no))
    if req.kind == "post":
        if not req.board or not re.match(BOARD_PATTERN, req.board) \
                or req.post_no is None or req.post_no < 0:
            raise HTTPException(status_code=400, detail="bad board or post_no")
        return ("kind = 'post' AND board = ? AND post_no = ?",
                (req.board, req.post_no))
    if req.kind == "file":
        if not req.file_md5 or not re.fullmatch(r"[0-9a-fA-F]{32}", req.file_md5):
            raise HTTPException(status_code=400, detail="bad file_md5")
        return ("kind = 'file' AND file_md5 = ?", (bytes.fromhex(req.file_md5),))
    raise HTTPException(status_code=400, detail="bad pin kind")


@app.post("/api/pin")
def add_pin(request: Request, req: PinReq):
    _require_local(request)
    _pin_target(req)   # validate
    if req.kind == "thread":
        w("INSERT INTO pins (kind, board, thread_no) VALUES ('thread', ?, ?) "
          "ON CONFLICT DO NOTHING", (req.board, req.thread_no))
    elif req.kind == "post":
        w("INSERT INTO pins (kind, board, post_no) VALUES ('post', ?, ?) "
          "ON CONFLICT DO NOTHING", (req.board, req.post_no))
    else:  # file
        w("INSERT INTO pins (kind, file_md5) VALUES ('file', ?) "
          "ON CONFLICT DO NOTHING", (bytes.fromhex(req.file_md5),))
    return {"pinned": True}


@app.delete("/api/pin")
def del_pin(request: Request, req: PinReq):
    _require_local(request)
    where, params = _pin_target(req)
    w(f"DELETE FROM pins WHERE {where}", params)
    return {"pinned": False}


