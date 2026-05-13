import os
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]

SMTP_HOST = "mail.gandi.net"
SMTP_PORT = 587
SMTP_USER = os.environ.get("SMTP_USER", "contact@sauterenfrance.com")
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]

# Comma-separated list of recipient emails
RECIPIENT_EMAILS = os.environ.get(
    "RECIPIENT_EMAILS",
    "contact@sauterenfrance.com,info@ln-ts.nl"
)
RECIPIENT_NAME = os.environ.get("RECIPIENT_NAME", "Marine en Anita")
SENDER_NAME = "MarineAI"

# Max individual listing pages to fetch per run for price re-checks
PRICE_RECHECK_LIMIT = 50
# Days before re-fetching a listing page to check price
PRICE_RECHECK_DAYS = 14

SITES = [
    {
        "name": "IAD - Audrey Dumont",
        "type": "iad",
        "url": "https://www.iadfrance.fr/conseiller-immobilier/audrey.dumont",
        "base_url": "https://www.iadfrance.fr",
    },
    {
        "name": "Agence Centrale",
        "type": "sitemap",
        "sitemap_url": "https://www.agcentraleimmo.com/sitemap.xml",
        # Match individual listing pages: /1552-bel-appartement.html
        "listing_pattern": r"agcentraleimmo\.com/\d{3,5}-[a-z]",
        "base_url": "https://www.agcentraleimmo.com",
    },
    {
        "name": "Châteaux & Patrimoine",
        "type": "sitemap",
        "sitemap_url": "https://www.chateauxetpatrimoine.com/proprietes-sitemap.xml",
        # Only French URLs, not /en/ duplicates
        "listing_pattern": r"chateauxetpatrimoine\.com/proprietes/[^/]+/$",
        "base_url": "https://www.chateauxetpatrimoine.com",
    },
    {
        "name": "PY Immobilier",
        "type": "sitemap",
        "sitemap_url": "http://www.py-immobilier.fr/sitemap.xml",
        # Match: /vente/836-lure/maison/8784-slug
        "listing_pattern": r"py-immobilier\.fr/vente/[^/]+/[^/]+/\d+",
        "base_url": "http://www.py-immobilier.fr",
    },
    {
        "name": "HSB Immobilier",
        "type": "hsb",
        "url": "http://hsbimmobilier.fr/biens.html",
        "base_url": "http://hsbimmobilier.fr",
    },
    {
        "name": "Sovimo",
        "type": "sitemap",
        "sitemap_url": "https://www.sovimo-immobilier-confolens.fr/sitemap.xml",
        # Match: /vente/1-confolens/maison/t5/3418-slug/
        "listing_pattern": r"sovimo.+/vente/[^/]+/[^/]+/[^/]+/\d+",
        "base_url": "https://www.sovimo-immobilier-confolens.fr",
    },
    {
        "name": "Beautiful South 66",
        "type": "sitemap",
        "sitemap_url": "https://www.beautifulsouth66.com/sitemap-1.xml",
        # Only French URLs
        "listing_pattern": r"beautifulsouth66\.com/fr/propriete/",
        "base_url": "https://www.beautifulsouth66.com",
    },
    {
        "name": "AMI09",
        "type": "sitemap",
        "sitemap_url": "https://www.ami09.com/product-sitemap.xml",
        "listing_pattern": r"ami09\.com/produit/\d+",
        "base_url": "https://www.ami09.com",
    },
    {
        "name": "Gélas Immobilier",
        "type": "sitemap",
        "sitemap_url": "https://gelas-immobilier.com/sitemap-1.xml",
        # Only French URLs
        "listing_pattern": r"gelas-immobilier\.com/fr/propriete/",
        "base_url": "https://gelas-immobilier.com",
    },
    {
        "name": "Max Immo",
        "type": "maximmo",
        "url": "https://www.maximmo.fr/SearchProperties/Search",
        "base_url": "https://www.maximmo.fr",
    },
]
