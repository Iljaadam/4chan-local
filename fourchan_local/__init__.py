"""fourchan-local: installable local-4chan mirror + browser.

Everything lives in this package: the `4cl` CLI (cli.py) supervises the poller
(poller.py), media worker (media.py), and web UI (app.py); db.py + retention.py
back them with a single SQLite file, and schema/templates/static ship as package
data so a plain wheel install runs with no source tree present.
"""
__version__ = "0.6.0"
