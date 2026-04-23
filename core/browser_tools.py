"""
Web browsing tools for Alfred.

Three public entry points:
  - fetch_page(url): GET a URL and return cleaned readable text.
  - search_web(query): hit the local SearXNG instance and return
    the top N results as structured dicts.
  - research(topic, depth): combine search + fetch to produce a
    digest Alfred can consume as context.

No pip deps. urllib + html.parser only. Works behind HTTPS_PROXY
when the env var is set.
"""

from __future__ import annotations

import json
import logging
import os
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Optional

log = logging.getLogger("alfred.browser")

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Alfred/1.0"
)
REQUEST_TIMEOUT = 20
MAX_BYTES = 2 * 1024 * 1024  # safety cap per page fetch
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")


# ---- opener -----------------------------------------------------------------

def _opener() -> urllib.request.OpenerDirector:
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"https": proxy, "http": proxy}))
    handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    return urllib.request.build_opener(*handlers)


# ---- page fetch -------------------------------------------------------------

SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "aside", "header",
             "svg", "form", "iframe"}
BLOCK_TAGS = {"p", "div", "section", "article", "li", "tr", "br",
              "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}


class _Reader(HTMLParser):
    """Strip markup, keep readable text with paragraph breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._title: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag in SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "title":
            self._in_title = True
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag == "br":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_title:
            self._title.append(data)
        self.parts.append(data)

    def extract(self) -> tuple[str, str]:
        raw = "".join(self.parts)
        # collapse whitespace per line, then strip repeat blank lines
        lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.splitlines()]
        cleaned: list[str] = []
        blank = False
        for ln in lines:
            if ln:
                cleaned.append(ln)
                blank = False
            elif not blank:
                cleaned.append("")
                blank = True
        title = " ".join("".join(self._title).split())
        return title, "\n".join(cleaned).strip()


@dataclass
class Page:
    url: str
    final_url: str
    status: int
    title: str
    text: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def fetch_page(url: str, *, max_chars: int = 20000) -> Page:
    """Fetch `url`, strip HTML, return cleaned text plus title.

    Text is capped at `max_chars` so downstream prompts stay bounded.
    Non-HTML responses are returned as-is (truncated).
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    opener = _opener()
    try:
        with opener.open(req, timeout=REQUEST_TIMEOUT) as resp:
            status = resp.status
            final_url = resp.geturl()
            ctype = resp.headers.get("Content-Type", "").lower()
            charset = _charset(ctype) or "utf-8"
            body = resp.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        return Page(url=url, final_url=url, status=exc.code, title="", text=f"[HTTP {exc.code}]")
    except urllib.error.URLError as exc:
        return Page(url=url, final_url=url, status=0, title="", text=f"[fetch failed: {exc}]")

    if "html" not in ctype and "xml" not in ctype:
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return Page(url=url, final_url=final_url, status=status, title="",
                    text=text[:max_chars])

    html = body.decode(charset, errors="replace")
    reader = _Reader()
    try:
        reader.feed(html)
    except Exception:
        log.exception("browser: HTML parse failed for %s", url)
    title, text = reader.extract()
    return Page(
        url=url,
        final_url=final_url,
        status=status,
        title=title,
        text=text[:max_chars],
    )


def _charset(ctype: str) -> Optional[str]:
    m = re.search(r"charset=([^\s;]+)", ctype)
    return m.group(1).strip().strip('"').strip("'") if m else None


# ---- search -----------------------------------------------------------------

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    engine: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def search_web(query: str, *, top_n: int = 5,
               searxng_url: str = SEARXNG_URL) -> list[SearchResult]:
    """Query SearXNG for `query`, return the top N results."""
    if not query.strip():
        return []
    params = urllib.parse.urlencode({"q": query, "format": "json", "safesearch": "1"})
    url = f"{searxng_url.rstrip('/')}/search?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json",
    })
    try:
        with _opener().open(req, timeout=REQUEST_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        log.warning("browser: search failed: %s", exc)
        return []
    except json.JSONDecodeError as exc:
        log.warning("browser: search returned non-JSON: %s", exc)
        return []

    results = []
    for item in payload.get("results", []):
        results.append(SearchResult(
            title=item.get("title", "").strip(),
            url=item.get("url", "").strip(),
            snippet=(item.get("content") or "").strip(),
            engine=item.get("engine", ""),
        ))
        if len(results) >= top_n:
            break
    return results


# ---- research ---------------------------------------------------------------

@dataclass
class ResearchBrief:
    topic: str
    depth: str
    results: list[SearchResult] = field(default_factory=list)
    pages: list[Page] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "depth": self.depth,
            "results": [r.as_dict() for r in self.results],
            "pages": [p.as_dict() for p in self.pages],
            "summary": self.summary,
        }


def research(topic: str, depth: str = "quick", *, fetch_n: int = 3) -> ResearchBrief:
    """Search the web and combine results into a brief.

    depth="quick": search only, use snippets. No page fetches.
    depth="deep":  fetch the top `fetch_n` results and include cleaned text.

    The returned `summary` field is plain text suitable for stuffing into
    a Claude prompt or showing in Telegram.
    """
    depth = depth if depth in ("quick", "deep") else "quick"
    results = search_web(topic, top_n=max(fetch_n, 5))
    brief = ResearchBrief(topic=topic, depth=depth, results=results)

    lines = [f"Research: {topic}", f"Depth: {depth}", ""]
    if not results:
        brief.summary = "\n".join(lines + ["No results."])
        return brief

    if depth == "quick":
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r.title}")
            lines.append(f"   {r.url}")
            if r.snippet:
                lines.append(f"   {r.snippet}")
            lines.append("")
    else:
        for i, r in enumerate(results[:fetch_n], 1):
            page = fetch_page(r.url, max_chars=4000)
            brief.pages.append(page)
            lines.append(f"{i}. {r.title or page.title}")
            lines.append(f"   {r.url}")
            if r.snippet:
                lines.append(f"   snippet: {r.snippet}")
            body = page.text.strip()
            if body:
                excerpt = body[:1200] + ("..." if len(body) > 1200 else "")
                lines.append(f"   excerpt: {excerpt}")
            lines.append("")

    brief.summary = "\n".join(lines).strip()
    return brief


# ---- test block -------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if "--offline" in sys.argv:
        # Exercise the HTML reader without making any network calls.
        sample_html = """
        <html><head><title>Test Page</title><style>body{}</style></head>
        <body>
          <nav>skip me</nav>
          <article>
            <h1>Headline</h1>
            <p>First paragraph with <b>bold</b> text.</p>
            <script>var x=1;</script>
            <p>Second paragraph.</p>
            <ul><li>one</li><li>two</li></ul>
          </article>
          <footer>skip</footer>
        </body></html>
        """
        reader = _Reader()
        reader.feed(sample_html)
        title, text = reader.extract()
        assert title == "Test Page", title
        assert "First paragraph with bold text." in text
        assert "skip me" not in text
        assert "var x" not in text
        print("title:", title)
        print("text:")
        print(text)
        print("----")
        print("offline tests: ok")
        sys.exit(0)

    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m core.browser_tools <url|--search query|--research topic>")

    if sys.argv[1] == "--search":
        for r in search_web(" ".join(sys.argv[2:])):
            print(r.title, "-", r.url)
            print(" ", r.snippet)
    elif sys.argv[1] == "--research":
        brief = research(" ".join(sys.argv[2:]), depth="quick")
        print(brief.summary)
    else:
        page = fetch_page(sys.argv[1])
        print("TITLE:", page.title)
        print("URL:", page.final_url)
        print("STATUS:", page.status)
        print()
        print(page.text[:2000])
