from __future__ import annotations
import logging

from .base import Listing, fetch

log = logging.getLogger(__name__)


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

    props = data.get("properties") or []
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
