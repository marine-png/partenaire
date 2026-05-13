from __future__ import annotations
import re
import json
import logging

from bs4 import BeautifulSoup

from .base import Listing, fetch, _parse_french_price

log = logging.getLogger(__name__)


def scrape(site: dict) -> list[Listing]:
    """Scrape Max Immo search results. All refs included — filter manually for 600./601."""
    r = fetch(site["url"])
    if not r:
        log.warning("Max Immo: fetch failed — check URL or site structure")
        return []

    # Try JSON response first
    content_type = r.headers.get("Content-Type", "")
    if "json" in content_type:
        return _parse_json(r.json(), site["base_url"])

    return _parse_html(r.text, site["base_url"])


def _parse_json(data: dict | list, base_url: str) -> list[Listing]:
    listings = []
    items = data if isinstance(data, list) else data.get("results", data.get("items", []))
    for item in items:
        url = item.get("url") or item.get("link") or item.get("Url") or ""
        if url and not url.startswith("http"):
            url = base_url.rstrip("/") + "/" + url.lstrip("/")
        ref = str(item.get("ref") or item.get("Ref") or item.get("reference") or "")
        title = item.get("title") or item.get("Title") or item.get("name") or ""
        price_raw = item.get("price") or item.get("Price") or item.get("prix") or 0
        try:
            price = int(float(str(price_raw).replace(" ", "").replace(".", "").replace(",", "")))
            price = price if 5_000 <= price <= 50_000_000 else None
        except (ValueError, TypeError):
            price = None
        if url:
            listings.append(Listing(url=url, ref=ref or None, title=title or None, price=price))
    log.info(f"  Max Immo (JSON): {len(listings)} listings")
    return listings


def _parse_html(html: str, base_url: str) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    listings = []
    seen = set()

    # Look for listing links — Max Immo uses /Property/Detail/ID pattern
    for a in soup.find_all("a", href=re.compile(r"/(Property|Detail|Bien|Annonce)/", re.I)):
        href = a.get("href", "")
        url = href if href.startswith("http") else base_url.rstrip("/") + href
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
            ref_match = re.search(r"[Nn]o\.?\s*[Dd]ossier\s*:?\s*([\d\.]+)", text)
            if ref_match:
                ref = ref_match.group(1)
            for tag in ["h2", "h3", "h4"]:
                el = card.find(tag)
                if el:
                    title = el.get_text(strip=True)
                    break

        listings.append(Listing(url=url, ref=ref, title=title, price=price))

    if not listings:
        log.warning(
            "Max Immo: no listings found in HTML. "
            "The site may use JavaScript rendering or a different URL pattern. "
            f"Page title: {soup.find('title') and soup.find('title').get_text()}"
        )

    log.info(f"  Max Immo (HTML): {len(listings)} listings")
    return listings
