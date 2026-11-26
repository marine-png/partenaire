from __future__ import annotations
import logging

from .base import Listing, fetch

log = logging.getLogger(__name__)


def _items(props) -> list[dict]:
    """Listings from the 'properties' field: a plain list (old API) or, since Nov 2026,
    {"type": ..., "all": {"items": [...], "totalItems": n}} — any nested "items" list."""
    if isinstance(props, list):
        return [p for p in props if isinstance(p, dict)]
    out: list[dict] = []
    if isinstance(props, dict):
        for k, v in props.items():
            if k == "items" and isinstance(v, list):
                out += [p for p in v if isinstance(p, dict)]
            elif isinstance(v, (dict, list)):
                out += _items(v) if isinstance(v, dict) else []
    seen: set = set()
    return [p for p in out if not (p.get("propertyListingRef") in seen or seen.add(p.get("propertyListingRef")))]


def scrape(site: dict) -> list[Listing]:
    """
    Fetch one IAD consultant's own listings via IAD's agent API
    (/api/agents/{username}). The HTML profile page also shows other agents'
    "biens similaires", so it's not used.
    """
    base = site["base_url"].rstrip("/")
    username = site["agent_username"]
    r = fetch(f"{base}/api/agents/{username}?locale=fr")
    if not r:
        return []

    try:
        data = r.json()
    except ValueError:
        log.error(f"  IAD: agent API did not return JSON for {username}")
        return []

    props = _items(data.get("properties"))
    expected = data.get("propertyCount")
    if expected is not None and expected != len(props):
        log.warning(f"  IAD: propertyCount={expected} but {len(props)} properties returned")

    listings: list[Listing] = []
    for p in props:
        ref = p.get("propertyListingRef")
        slug = (p.get("slugs") or {}).get("fr")
        if not ref or not slug:
            continue
        price = (p.get("price") or {}).get("main")
        listings.append(Listing(
            url=f"{base}/annonce/{slug}/r{ref}",
            ref=f"r{ref}",
            title=p.get("title"),  # e.g. "Maison à Mouzon (08210)"
            price=int(price) if price else None,
        ))

    log.info(f"  IAD - {username}: {len(listings)} listings")
    return listings
