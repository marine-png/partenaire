from __future__ import annotations
import re
import logging
from xml.etree import ElementTree as ET

from .base import Listing, fetch

log = logging.getLogger(__name__)

NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def scrape(site: dict) -> list[Listing]:
    """Parse a sitemap XML and return listings matching the configured pattern."""
    sitemap_url = site["sitemap_url"]
    pattern = site.get("listing_pattern", "")

    r = fetch(sitemap_url)
    if not r:
        log.error(f"Could not fetch sitemap: {sitemap_url}")
        return []

    try:
        root = ET.fromstring(r.content)
    except ET.ParseError as e:
        log.error(f"XML parse error for {sitemap_url}: {e}")
        return []

    listings: list[Listing] = []
    for url_el in root.findall(".//sm:url", NS):
        loc = url_el.findtext("sm:loc", namespaces=NS) or ""
        lastmod = url_el.findtext("sm:lastmod", namespaces=NS)

        if pattern and not re.search(pattern, loc):
            continue

        listings.append(Listing(url=loc, lastmod=lastmod))

    log.info(f"  {site['name']}: {len(listings)} listings in sitemap")
    return listings
