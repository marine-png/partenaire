from __future__ import annotations
import re
import logging

from bs4 import BeautifulSoup

from .base import Listing, fetch, _parse_french_price

log = logging.getLogger(__name__)


def scrape(site: dict) -> list[Listing]:
    """Scrape HSB Immobilier listing page."""
    r = fetch(site["url"])
    if not r:
        return []

    soup = BeautifulSoup(r.text, "lxml")
    base = site["base_url"]
    listings: list[Listing] = []
    seen = set()

    # HSB URLs: biens-470-propriete-isolee-sur-3ha.html
    for a in soup.find_all("a", href=re.compile(r"biens-\d+-")):
        href = a.get("href", "")
        url = href if href.startswith("http") else base.rstrip("/") + "/" + href.lstrip("/")
        if url in seen:
            continue
        seen.add(url)

        card = a.find_parent(["article", "div", "li", "tr"])
        price = None
        title = None
        ref = None

        if card:
            text = card.get_text(" ", strip=True)
            price = _parse_french_price(text)
            # Reference pattern: ML2097, JD2095, etc.
            ref_match = re.search(r"\b([A-Z]{2}\d{4})\b", text)
            if ref_match:
                ref = ref_match.group(1)
            for tag in ["h2", "h3", "h4", "strong"]:
                el = card.find(tag)
                if el:
                    title = el.get_text(strip=True)
                    break

        listings.append(Listing(url=url, ref=ref, title=title, price=price))

    log.info(f"  HSB Immobilier: {len(listings)} listings")
    return listings
