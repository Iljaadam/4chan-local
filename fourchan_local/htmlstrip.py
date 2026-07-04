"""Convert 4chan comment HTML ('com' field) to plaintext for FTS + display.

4chan uses a small, stable subset: <br>, <a class="quotelink">, <span class="quote">,
<s> (spoiler), <pre> (code), <wbr>. We turn <br> into newlines, drop tags, and
unescape entities. Keeps it dependency-free.
"""
import html
import re

_BR = re.compile(r"<br\s*/?>", re.I)
_WBR = re.compile(r"<wbr\s*/?>", re.I)
_TAG = re.compile(r"<[^>]+>")


def strip(com: str | None) -> str:
    if not com:
        return ""
    s = _BR.sub("\n", com)
    s = _WBR.sub("", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    return s.strip()
