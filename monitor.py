#!/usr/bin/env python3
"""
Partner website monitor.
Detects new/removed listings and price changes across 10 partner sites.
Sends a Dutch-language email summary when changes are found.
"""
from __future__ import annotations
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client

import config
from scrapers import sitemap, iad, hsb, maximmo
from scrapers.base import Listing, fetch, extract_price
from notifier import send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Scraper dispatch
# ---------------------------------------------------------------------------

def scrape_site(site: dict) -> list[Listing]:
    scraper_map = {
        "sitemap": sitemap.scrape,
        "iad": iad.scrape,
        "hsb": hsb.scrape,
        "maximmo": maximmo.scrape,
    }
    fn = scraper_map.get(site["type"])
    if not fn:
        log.error(f"Unknown scraper type '{site['type']}' for {site['name']}")
        return []
    try:
        return fn(site)
    except Exception as e:
        log.error(f"Scraper crashed for {site['name']}: {e}")
        return []


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def get_active_listings(db: Client, site_name: str) -> dict[str, dict]:
    result = (
        db.table("partner_listings")
        .select("*")
        .eq("site_name", site_name)
        .eq("status", "active")
        .execute()
    )
    return {row["listing_url"]: row for row in result.data}


def insert_listing(db: Client, site_name: str, listing: Listing) -> None:
    db.table("partner_listings").insert({
        "site_name": site_name,
        "listing_url": listing.url,
        "ref": listing.ref,
        "title": listing.title,
        "price": listing.price,
        "sitemap_lastmod": listing.lastmod,
        "status": "active",
        "last_price_checked_at": NOW.isoformat() if listing.price else None,
    }).execute()


def touch_listing(db: Client, row_id: int, listing: Listing) -> None:
    update = {
        "last_seen_at": NOW.isoformat(),
        "sitemap_lastmod": listing.lastmod,
    }
    if listing.title:
        update["title"] = listing.title
    db.table("partner_listings").update(update).eq("id", row_id).execute()


def mark_removed(db: Client, row_id: int) -> None:
    db.table("partner_listings").update({
        "status": "removed",
        "last_seen_at": NOW.isoformat(),
    }).eq("id", row_id).execute()


def update_price(db: Client, row: dict, new_price: int) -> None:
    history = row.get("price_history") or []
    history.append({"price": row["price"], "changed_at": NOW.isoformat()})
    db.table("partner_listings").update({
        "price": new_price,
        "price_history": json.dumps(history),
        "last_seen_at": NOW.isoformat(),
        "last_price_checked_at": NOW.isoformat(),
    }).eq("id", row["id"]).execute()


def record_price_checked(db: Client, row_id: int) -> None:
    db.table("partner_listings").update({
        "last_price_checked_at": NOW.isoformat(),
    }).eq("id", row_id).execute()


# ---------------------------------------------------------------------------
# Price fetching for sitemap-based listings
# ---------------------------------------------------------------------------

def fetch_listing_price(url: str) -> int | None:
    r = fetch(url)
    if not r:
        return None
    return extract_price(r.text)


def needs_price_recheck(row: dict) -> bool:
    last = row.get("last_price_checked_at")
    if not last:
        return True
    try:
        checked_at = datetime.fromisoformat(last.replace("Z", "+00:00"))
        return (NOW - checked_at) > timedelta(days=config.PRICE_RECHECK_DAYS)
    except (ValueError, TypeError):
        return True


# ---------------------------------------------------------------------------
# Per-site processing
# ---------------------------------------------------------------------------

def process_site(db: Client, site: dict, price_recheck_budget: list[int]) -> dict:
    """
    Returns changes = {
        "new": [Listing, ...],
        "removed": [db_row, ...],
        "price_changed": [(Listing, old_price, new_price), ...],
    }
    price_recheck_budget is a mutable list[int] shared across sites to cap total fetches.
    """
    changes: dict = {"new": [], "removed": [], "price_changed": []}
    site_name = site["name"]

    log.info(f"→ {site_name}")
    current_listings = scrape_site(site)
    if not current_listings:
        log.warning(f"  No listings returned — skipping DB update")
        return changes

    current_by_url = {l.url: l for l in current_listings}
    stored_by_url = get_active_listings(db, site_name)

    is_sitemap_site = (site["type"] == "sitemap")

    # ---- New listings ----
    for url, listing in current_by_url.items():
        if url in stored_by_url:
            continue
        # For sitemap sites, fetch page to get title + price
        if is_sitemap_site and price_recheck_budget[0] > 0:
            price = fetch_listing_price(url)
            listing = Listing(
                url=url,
                ref=listing.ref,
                title=listing.title,
                price=price,
                lastmod=listing.lastmod,
            )
            price_recheck_budget[0] -= 1
        insert_listing(db, site_name, listing)
        changes["new"].append(listing)
        log.info(f"  + NEW: {url}")

    # ---- Removed listings ----
    for url, row in stored_by_url.items():
        if url not in current_by_url:
            mark_removed(db, row["id"])
            changes["removed"].append(row)
            log.info(f"  - REMOVED: {url}")

    # ---- Existing listings: price checks ----
    for url, listing in current_by_url.items():
        if url not in stored_by_url:
            continue  # already handled as new

        row = stored_by_url[url]

        # HTML scrapers already have prices — check directly
        if not is_sitemap_site:
            new_price = listing.price
        else:
            # Sitemap sites: only re-fetch if lastmod changed or due for recheck
            lastmod_changed = (
                listing.lastmod
                and row.get("sitemap_lastmod")
                and listing.lastmod != row["sitemap_lastmod"]
            )
            if (lastmod_changed or needs_price_recheck(row)) and price_recheck_budget[0] > 0:
                new_price = fetch_listing_price(url)
                price_recheck_budget[0] -= 1
                if new_price is not None:
                    record_price_checked(db, row["id"])
            else:
                new_price = None

        old_price = row.get("price")
        if new_price is not None and old_price is not None and new_price != old_price:
            update_price(db, row, new_price)
            changes["price_changed"].append((listing, old_price, new_price))
            log.info(f"  € PRICE: {url} {old_price} → {new_price}")
        else:
            touch_listing(db, row["id"], listing)

    return changes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=== Partner monitor started ===")
    db = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

    # Shared budget for individual page fetches (price extraction)
    price_recheck_budget = [config.PRICE_RECHECK_LIMIT]

    all_changes: dict[str, dict] = {}
    for site in config.SITES:
        try:
            changes = process_site(db, site, price_recheck_budget)
            if any(changes.values()):
                all_changes[site["name"]] = changes
        except Exception as e:
            log.error(f"Fatal error processing {site['name']}: {e}")

    if all_changes:
        total = sum(
            len(c["new"]) + len(c["removed"]) + len(c["price_changed"])
            for c in all_changes.values()
        )
        log.info(f"=== {total} changes across {len(all_changes)} sites — sending email ===")
        send_email(all_changes)
    else:
        log.info("=== No changes detected — no email sent ===")


if __name__ == "__main__":
    main()
