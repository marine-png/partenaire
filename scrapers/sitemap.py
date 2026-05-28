from __future__ import annotations
import re
import logging
from xml.etree import ElementTree as ET

from .base import Listing, fetch

log = logging.getLogger(__name__)

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

    # Auto-detect namespace from root tag (handles both http:// and https:// variants)
    tag = root.tag
    if "{" in tag:
        ns_uri = tag.split("}")[0][1:]
        NS = {"sm": ns_uri}
        url_elements = root.findall(".//sm:url", NS)
        def get_loc(el): return el.findtext("sm:loc", namespaces=NS) or ""
        def get_lastmod(el): return el.findtext("sm:lastmod", namespaces=NS)
    else:
        url_elements = root.findall(".//url")
        def get_loc(el): return el.findtext("loc") or ""
        def get_lastmod(el): return el.findtext("lastmod")

    listings: list[Listing] = []
    for url_el in url_elements:
        loc = get_loc(url_el)
        lastmod = get_lastmod(url_el)

        if pattern and not re.search(pattern, loc):
            continue

        listings.append(Listing(url=loc, lastmod=lastmod))

    log.info(f"  {site['name']}: {len(listings)} listings in sitemap")
    return listings
