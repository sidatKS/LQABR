"""HTML -> readable article text. Ported from blog-summarizer's crawler.

Kept as its own module because two adapters need it: `url` always, and
`api` whenever an endpoint answers with HTML instead of JSON.
"""

from __future__ import annotations

from typing import Tuple

from bs4 import BeautifulSoup

#: Chrome, not a bot string. Some publishers serve an interstitial to
#: anything that admits to being a script, and an interstitial summarised as
#: an article is worse than a failed fetch.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")

_NOISE = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript")


def extract(html: str) -> Tuple[str, str]:
    """(title, article text). Navigation, ads, menus and comments removed."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    heading = soup.find("h1")
    if heading:
        heading_text = heading.get_text(strip=True)
        # The <h1> is the article's own headline; <title> usually carries the
        # site name too. Prefer the headline when we have one.
        title = heading_text or title

    for element in soup(_NOISE):
        element.decompose()

    article = soup.find("article")
    body = article if article else soup
    content = body.get_text(separator="\n", strip=True)

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    return title, "\n".join(lines)
