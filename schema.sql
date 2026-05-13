-- Run this in your Supabase SQL editor before first use.

CREATE TABLE IF NOT EXISTS partner_listings (
    id                    BIGSERIAL PRIMARY KEY,
    site_name             TEXT NOT NULL,
    listing_url           TEXT NOT NULL,
    ref                   TEXT,
    title                 TEXT,
    price                 INTEGER,                       -- euros, integer
    price_history         JSONB NOT NULL DEFAULT '[]',  -- [{price, changed_at}]
    status                TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'removed')),
    sitemap_lastmod       TEXT,                         -- lastmod from sitemap
    first_seen_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_price_checked_at TIMESTAMPTZ,
    UNIQUE (site_name, listing_url)
);

CREATE INDEX IF NOT EXISTS idx_partner_listings_site_status
    ON partner_listings (site_name, status);

CREATE INDEX IF NOT EXISTS idx_partner_listings_price_check
    ON partner_listings (last_price_checked_at)
    WHERE status = 'active';
