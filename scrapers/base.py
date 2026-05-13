from __future__ import annotations
import re
import json
import logging
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


@dataclass
class Listing:
    url: str
    ref: str | None = None
    title: str | None = None
    price: int | None = None       # euros
    lastmod: str | None = None     # ISO date string from sitemap


def fetch(url: str, timeout: int = 15) -> requests.Response | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning(f"Fetch failed for {url}: {e}")
        return None


def extract_price(html: str) -> int | None:
    """Generic price extractor. Tries JSON-LD, meta tags, CSS classes, then regex."""
    soup = BeautifulSoup(html, "lxml")

    # 1. JSON-LD schema.org
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            offers = data.get("offers", {})
            price_val = offers.get("price") or offers.get("lowPrice") or data.get("price")
            if price_val:
                p = int(float(str(price_val).replace(",", ".")))
                if 5_000 <= p <= 50_000_000:
                    return p
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError):
            continue

    # 2. Common price CSS selectors
    for sel in [
        "[itemprop='price']", ".price", ".prix", ".bien-prix",
        "[class*='price']", "[class*='prix']", ".listing-price",
    ]:
        el = soup.select_one(sel)
        if el:
            p = _parse_french_price(el.get_text(" ", strip=True))
            if p:
                return p

    # 3. Regex on full text
    return _parse_french_price(soup.get_text(" ", strip=True))


def _parse_french_price(text: str) -> int | None:
    """Parse French price formats: 275 000 €, 275.000 €, €275000, etc."""
    patterns = [
        r"(\d[\d\s\.]{3,9})\s*€",   # 275 000 € or 275.000 €
        r"€\s*(\d[\d\s\.]{3,9})",   # € 275 000
        r"(\d{5,8})\s*€",            # 275000€
    ]
    for pat in patterns:
        for m in re.findall(pat, text):
            try:
                p = int(re.sub(r"[\s\.]", "", m))
                if 5_000 <= p <= 50_000_000:
                    return p
            except ValueError:
                continue
    return None
