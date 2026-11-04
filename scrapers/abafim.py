from __future__ import annotations
import re
import logging

from bs4 import BeautifulSoup
from .base import Listing, fetch, extract_price

log = logging.getLogger(__name__)

BASE_URL = "https://abafim.fr"
LISTING_PAGE = "/fr/acheter"

# Match individual FR listing URLs: /fr/propriete/vente+[type]+[ville]+[slug]+[8-digit-id]
_LISTING_RE = re.compile(r"/fr/propriete/vente\+[^\"'\s]+\+(\d{7,9})$")


def scrape(site: dict) -> list[Listing]:
    listings: list[Listing] = []
    page = 1

    while True:
        url = f"{BASE_URL}{LISTING_PAGE}?page={page}"
        r = fetch(url)
        if not r:
            log.error(f"[abafim] Failed to fetch page {page}")
            break

        soup = BeautifulSoup(r.text, "lxml")
        found = _extract_listings(soup)

        if not found:
            break  # no more pages

        listings.extend(found)
        log.info(f"[abafim] Page {page}: {len(found)} listings")

        # Check if there's a next page
        if not _has_next_page(soup, page):
            break
        page += 1

    log.info(f"[abafim] Total: {len(listings)} listings")
    return listings


def _extract_listings(soup: BeautifulSoup) -> list[Listing]:
    results = []
    seen_ids: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = _LISTING_RE.search(href)
        if not m:
            continue

        numeric_id = m.group(1)
        if numeric_id in seen_ids:
            continue
        seen_ids.add(numeric_id)

        absolute_url = BASE_URL + href if href.startswith("/") else href

        # Try to extract price from the card itself
        price = _extract_card_price(a)

        # Extract title from the link text or nearby heading
        title = _extract_card_title(a)

        results.append(Listing(
            url=absolute_url,
            ref=f"ABF-{numeric_id}",
            title=title,
            price=price,
        ))

    return results


def _extract_card_price(tag) -> int | None:
    """Extract price from the listing card element."""
    from .base import _parse_french_price
    # Look for price in the card and its siblings/parent
    container = tag.parent or tag
    for _ in range(3):  # walk up max 3 levels
        text = container.get_text(" ", strip=True)
        price = _parse_french_price(text)
        if price:
            return price
        container = container.parent
        if container is None:
            break
    return None


def _extract_card_title(tag) -> str | None:
    # Prefer <h2>/<h3> inside the card; fallback to img alt; fallback to link text
    for heading_tag in ["h2", "h3", "h4"]:
        h = tag.find(heading_tag)
        if h:
            return h.get_text(strip=True)
    img = tag.find("img")
    if img and img.get("alt"):
        return img["alt"].strip()
    text = tag.get_text(" ", strip=True)[:80]
    return text or None


def _has_next_page(soup: BeautifulSoup, current_page: int) -> bool:
    """Check if a next-page link exists."""
    next_page_num = current_page + 1
    # Look for ?page=N+1 link anywhere on the page
    for a in soup.find_all("a", href=True):
        if f"page={next_page_num}" in a["href"]:
            return True
    return False


def fetch_listing_price(url: str) -> int | None:
    """Fetch an individual listing page and extract price."""
    r = fetch(url)
    if not r:
        return None
    return extract_price(r.text)
