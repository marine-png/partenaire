#!/usr/bin/env python3
"""
Partner website monitor.
Detects new/removed listings and price changes across partner sites.
Sends a Dutch-language email with price changes, alerts (broken site/scraper),
and new/removed listings for the sites configured with "notify" in config.py.
"""
from __future__ import annotations
import json
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

from supabase import create_client, Client

import config
from scrapers import sitemap, iad, hsb, maximmo, abafim
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
        "abafim": abafim.scrape,
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

def get_listings(db: Client, site_name: str) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (active_by_url, removed_by_url) for a site."""
    result = (
        db.table("partner_listings")
        .select("*")
        .eq("site_name", site_name)
        .execute()
    )
    active, removed = {}, {}
    for row in result.data:
        (active if row["status"] == "active" else removed)[row["listing_url"]] = row
    return active, removed


def reactivate_listing(db: Client, row_id: int, listing: Listing) -> None:
    """A listing previously marked removed is back online (UNIQUE url → can't re-insert)."""
    update = {
        "status": "active",
        "last_seen_at": NOW.isoformat(),
        "sitemap_lastmod": listing.lastmod,
    }
    if listing.title:
        update["title"] = listing.title
    if listing.price:
        update["price"] = listing.price
        update["last_price_checked_at"] = NOW.isoformat()
    db.table("partner_listings").update(update).eq("id", row_id).execute()


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
    # Older rows stored the history as a JSON string instead of a JSONB array
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except ValueError:
            history = []
    history.append({"price": row["price"], "changed_at": NOW.isoformat()})
    db.table("partner_listings").update({
        "price": new_price,
        "price_history": history,
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
# Sold pages (sites that keep sold listings online with a "vendu" label)
# ---------------------------------------------------------------------------

def listing_id(url: str) -> str | None:
    """Numeric property ID from the last path segment: /vente/.../3418-slug/ → 3418."""
    m = re.match(r"^(\d+)-", url.rstrip("/").split("/")[-1])
    if m and int(m.group(1)) > 10:  # exclude pagination numbers
        return m.group(1)
    return None


def fetch_sold_ids(sold_page_tpl: str, max_pages: int = 200) -> set[str]:
    sold: set[str] = set()
    for page in range(1, max_pages + 1):
        r = fetch(sold_page_tpl.format(page=page))
        if not r:
            break
        page_ids = {
            pid for href in re.findall(r'href="([^"]*/vente/[^"]+)"', r.text)
            if (pid := listing_id(href))
        }
        if not page_ids or page_ids <= sold:  # empty or repeating last page
            break
        sold |= page_ids
    return sold


# ---------------------------------------------------------------------------
# Per-site processing
# ---------------------------------------------------------------------------

def process_site(db: Client, site: dict, price_recheck_budget: list[int]) -> dict:
    """
    Returns changes = {
        "new": [Listing, ...],
        "removed": [db_row, ...],
        "price_changed": [(Listing, old_price, new_price), ...],
        "sold": [db_row, ...],
        "alerts": [str, ...],
    }
    price_recheck_budget is a mutable list[int] shared across sites to cap total fetches.
    """
    changes: dict = {"new": [], "removed": [], "price_changed": [], "sold": [], "alerts": []}
    site_name = site["name"]

    log.info(f"→ {site_name}")
    current_listings = scrape_site(site)
    stored_by_url, removed_by_url = get_listings(db, site_name)

    if not current_listings:
        log.warning(f"  No listings returned — skipping DB update")
        if stored_by_url:
            changes["alerts"].append(
                f"0 advertenties gevonden (vorige keer {len(stored_by_url)}). "
                f"Website of sitemap is waarschijnlijk veranderd — controleren."
            )
        return changes

    # First time we see this site: store everything silently (no "new" flood)
    first_sync = not stored_by_url and not removed_by_url
    if first_sync:
        log.info(f"  First sync — storing {len(current_listings)} listings without notification")

    # ---- Sold page: sold listings stay online, so take them out of "current" ----
    sold_ids: set[str] = set()
    if site.get("sold_page"):
        sold_ids = fetch_sold_ids(site["sold_page"])
        log.info(f"  {len(sold_ids)} sold IDs on sold page")
        if not sold_ids:
            changes["alerts"].append(
                f"Pagina met verkochte panden niet bereikbaar of leeg ({site['sold_page'].format(page=1)}) — "
                f"verkopen worden niet gedetecteerd."
            )
        current_listings = [l for l in current_listings if listing_id(l.url) not in sold_ids]

    current_by_url = {l.url: l for l in current_listings}

    is_sitemap_site = (site["type"] == "sitemap")

    # ---- Sold (explicitly listed on the sold page) ----
    sold_urls = [url for url in stored_by_url if sold_ids and listing_id(url) in sold_ids]
    for url in sold_urls:
        mark_removed(db, stored_by_url[url]["id"])
        changes["sold"].append(stored_by_url.pop(url))
        log.info(f"  ✓ SOLD: {url}")
    if len(changes["sold"]) > config.SOLD_BULK_LIMIT:
        # Archive backlog on first use of the sold page — not real news
        changes["alerts"].append(
            f"{len(changes['sold'])} oude verkochte panden in één keer bijgewerkt (eerste synchronisatie)."
        )
        changes["sold"] = []

    # ---- Mass-removal guard ----
    gone = [url for url in stored_by_url if url not in current_by_url]
    mass_removal = (
        len(gone) >= config.MASS_REMOVAL_MIN_COUNT
        and len(gone) > config.MASS_REMOVAL_RATIO * len(stored_by_url)
    )
    if mass_removal:
        log.warning(f"  {len(gone)}/{len(stored_by_url)} listings gone — not marking removed")
        changes["alerts"].append(
            f"{len(gone)} van {len(stored_by_url)} advertenties tegelijk verdwenen. "
            f"Waarschijnlijk een probleem met de website — niets als verwijderd gemarkeerd, controleren."
        )

    # ---- New listings ----
    for url, listing in current_by_url.items():
        if url in stored_by_url:
            continue
        if url in removed_by_url:
            reactivate_listing(db, removed_by_url[url]["id"], listing)
            log.info(f"  ↺ BACK ONLINE: {url}")
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
        if not first_sync:
            changes["new"].append(listing)
            log.info(f"  + NEW: {url}")

    # ---- Removed listings ----
    if not mass_removal:
        for url in gone:
            row = stored_by_url[url]
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
        except Exception as e:
            log.error(f"Fatal error processing {site['name']}: {e}")
            changes = {"new": [], "removed": [], "price_changed": [], "sold": [],
                       "alerts": [f"Technische fout tijdens de controle: {e}"]}

        # Price changes + alerts always; new/removed/sold only where Make doesn't cover them
        notify = set(site.get("notify", []))
        reported = {
            "new": changes["new"] if "new" in notify else [],
            "removed": changes["removed"] if "removed" in notify else [],
            "sold": changes["sold"] if "sold" in notify else [],
            "price_changed": changes["price_changed"],
            "alerts": changes["alerts"],
        }
        if any(reported.values()):
            all_changes[site["name"]] = reported

    log.info(f"=== Price recheck budget left: {price_recheck_budget[0]}/{config.PRICE_RECHECK_LIMIT} ===")
    if all_changes:
        total = sum(
            len(c["new"]) + len(c["removed"]) + len(c["price_changed"]) + len(c["sold"]) + len(c["alerts"])
            for c in all_changes.values()
        )
        log.info(f"=== {total} items to report across {len(all_changes)} sites — sending email ===")
        send_email(all_changes)
    else:
        log.info("=== Nothing to report — no email sent ===")


if __name__ == "__main__":
    main()
