from __future__ import annotations
import re
import logging

from bs4 import BeautifulSoup

from .base import Listing, fetch, _parse_french_price

log = logging.getLogger(__name__)


def scrape(site: dict) -> list[Listing]:
    """Scrape IAD France consultant profile page."""
    r = fetch(site["url"])
    if not r:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    base = site["base_url"]
    listings: list[Listing] = []

    # IAD listing cards: links containing /annonce/ with a ref like r1234567
    seen = set()
    for a in soup.find_all("a", href=re.compile(r"/annonce/")):
        href = a.get("href", "")
        # Extract ref from URL end: /r1986326
        ref_match = re.search(r"/(r\d+)$", href)
        ref = ref_match.group(1) if ref_match else None

        url = href if href.startswith("http") else base + href
        if url in seen:
            continue
        seen.add(url)

        # Try to find price within the same card ancestor
        card = a.find_parent(["article", "div", "li", "section"])
        price = None
        if card:
            price = _parse_french_price(card.get_text(" ", strip=True))

        # Try to find title
        title = None
        if card:
            for tag in ["h2", "h3", "h4"]:
                el = card.find(tag)
                if el:
                    title = el.get_text(strip=True)
                    break

        listings.append(Listing(url=url, ref=ref, title=title, price=price))

    log.info(f"  IAD - Audrey Dumont: {len(listings)} listings")
    return listings
