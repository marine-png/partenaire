from __future__ import annotations
import re
import logging

from bs4 import BeautifulSoup

from .base import Listing, fetch, _parse_french_price

log = logging.getLogger(__name__)

MAX_PAGES = 100


def scrape(site: dict) -> list[Listing]:
    """
    Agency sites built on the Apimo website CMS (Gélas, Beautiful South, Abafim).
    Their sitemap keeps sold listings, so read the paginated "for sale" search
    results instead (= what the reference search on the home page returns):
    a listing missing from these pages is sold or withdrawn.
    Card = <li data-property-id="N"> with an <a href="/fr/propriete/..."> and a .price.
    """
    base = site["base_url"].rstrip("/")
    listings: list[Listing] = []
    seen: set[str] = set()

    for page in range(1, MAX_PAGES + 1):
        r = fetch(f"{site['list_url']}?page={page}")
        if not r:
            log.warning(f"  {site['name']}: fetch failed on page {page}")
            return [] if page == 1 else listings  # partial list would look like mass removal

        soup = BeautifulSoup(r.text, "lxml")
        new_on_page = 0
        for card in soup.select("[data-property-id]"):
            pid = card["data-property-id"]
            a = card.find("a", href=re.compile(r"/fr/propriete/"))
            if pid in seen or not a:
                continue
            seen.add(pid)
            new_on_page += 1

            price_el = card.select_one(".price")
            title_el = card.find(["h2", "h3"])
            listings.append(Listing(
                url=a["href"] if a["href"].startswith("http") else base + a["href"],
                ref=pid,
                title=title_el.get_text(" ", strip=True) if title_el else None,
                price=_parse_french_price(price_el.get_text(" ", strip=True)) if price_el else None,
            ))
        if not new_on_page:  # past the last page (sites repeat the last page)
            break

    log.info(f"  {site['name']}: {len(listings)} listings for sale")
    return listings
