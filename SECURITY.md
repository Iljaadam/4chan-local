# Security policy

`4chan-local` is a single-user tool that runs on `localhost`. The web UI binds to
`127.0.0.1` only and the write (pin) endpoints reject non-loopback clients, so there
is no intended remote attack surface. Don't expose it to a network; it isn't hardened
or authenticated for that.

## Reporting a vulnerability

Please report security issues privately via GitHub's **"Report a vulnerability"**
(Security → Advisories) on the repository, rather than opening a public issue.
Include steps to reproduce and the affected version. I'll acknowledge and respond as
time permits — this is a hobby project, not a product with an SLA.

## Scope notes

- The app renders archived post content; it escapes HTML and sanitizes quote/link
  markup server-side, and sets a strict CSP. XSS or CSP-bypass reports are in scope.
- Path/traversal or injection reachable from a board/thread/search parameter is in
  scope.
- "It can download objectionable content from 4chan" is not a vulnerability — it's
  inherent to what the tool does. See the README's legal notice and the default-on
  media blocklist.
