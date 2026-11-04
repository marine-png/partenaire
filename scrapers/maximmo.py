from __future__ import annotations
import re
import logging

from bs4 import BeautifulSoup

from .base import Listing, fetch, _parse_french_price

log = logging.getLogger(__name__)

MAX_PAGES = 20


def scrape(site: dict) -> list[Listing]:
    """
    Scrape Max Immo search results (paginated, 30 per page).
    Card = <a href="/ShowProperty?propertyId=N"> with head "REF | CITY" and a "Prix" picto.
    Only refs starting with one of site["ref_prefixes"] (e.g. "600.", "601.") are kept.
    """
    prefixes = tuple(site.get("ref_prefixes", []))
    base = site["base_url"].rstrip("/")
    listings: list[Listing] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        r = fetch(f"{site['url']}?page={page}")
        if not r:
            log.warning(f"Max Immo: fetch failed on page {page}")
            return [] if page == 1 else listings

        soup = BeautifulSoup(r.text, "lxml")
        cards = soup.find_all("a", href=re.compile(r"ShowProperty\?propertyId=\d+"))
        if not cards:
            break

        for a in cards:
            url = base + "/" + a["href"].lstrip("/")
            if url in seen:
                continue
            seen.add(url)

            head = a.select_one(".property-summary-head")
            head_text = head.get_text(" ", strip=True) if head else ""
            ref_match = re.match(r"(\d{3}\.\d{5})", head_text)
            ref = ref_match.group(1) if ref_match else None
            if prefixes and not (ref and ref.startswith(prefixes)):
                continue

            # Price has its own picto block — don't parse the whole card
            # (rooms count sits right before the price: "3 420 000 €")
            price_block = a.find(attrs={"title": "Prix"})
            price = _parse_french_price(price_block.get_text(" ", strip=True)) if price_block else None

            img = a.find("img", alt=True)
            city = head_text.split("|", 1)[1].strip() if "|" in head_text else ""
            title = (img["alt"].strip().capitalize() if img and img["alt"].strip() else None)
            if title and city:
                title = f"{title} — {city.title()}"

            listings.append(Listing(url=url, ref=ref, title=title, price=price))

    log.info(f"  Max Immo: {len(listings)} listings (prefixes {prefixes or 'all'})")
    return listings
