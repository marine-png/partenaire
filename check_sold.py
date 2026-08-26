"""
Sold check quotidien — vérifie le statut des biens 'En cours' dans le Google Sheet Apimo.
- Met en gras les lignes vendues dans le sheet.
- Envoie un email récapitulatif.

Règles par partenaire (HTTP uniquement, sans Playwright) :
  AC, DA, AD, AUDR, SOI, TCY, ABF, BFS  → redirect vers homepage = vendu
  SOV                                    → page biens-vendus + matching par ID numérique
  CPA                                    → texte "aucun résultat"
  CPSB                                   → texte "page non trouvée"
  MI, Mi                                 → texte "le bien cherché n'existe pas"
  MIH, ISB, LV, WE                       → 404 ou page "not found"
  GCI                                    → page biens-vendus + URL matching
  OBI                                    → skip (Cloudflare, invérifiable)
  FSNL                                   → skip
"""
from __future__ import annotations
import json
import logging
import os
import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import urlparse

import requests
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import config

log = logging.getLogger(__name__)

SPREADSHEET_ID = "1D2x7g1plrlHN0sYivNp33g0oKcK_NnheZaWi47sr5D4"
SHEET_NAME = "fiches_produits_apimo.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
}

# Codes à ignorer totalement
SKIP_CODES = {"FSNL", "OBI"}

# Redirect vers homepage = vendu
HOMEPAGE_REDIRECT_CODES = {"AC", "DA", "AD", "AUDR", "SOI", "TCY", "ABF", "BFS"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def is_homepage(original_url: str, final_url: str) -> bool:
    try:
        orig = urlparse(original_url)
        final = urlparse(final_url)
        # Different domain = redirect away = sold
        if orig.netloc.lstrip("www.") != final.netloc.lstrip("www."):
            return True
        path = final.path.strip("/").lower()
        # Homepage / language root
        if path in ("", "fr", "en", "nl", "accueil", "home"):
            return True
        # Explicit not-found / error paths (ABF → /fr/not-found, etc.)
        if any(kw in path for kw in ("not-found", "not_found", "404", "introuvable", "page-non-trouvee")):
            return True
        # Listing catalog redirects (IAD → /annonces/vente, etc.)
        if any(kw in path for kw in ("annonces/vente", "annonces-vente", "biens-a-vendre", "catalogue")):
            return True
        return False
    except Exception:
        return False


def http_check(url: str, timeout: int = 12):
    """Returns (status_code, final_url, text_lower)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return r.status_code, r.url, r.text.lower()
    except requests.exceptions.Timeout:
        return None, None, "__timeout__"
    except Exception as e:
        return None, None, f"__error__{e}"


# ─── Per-partner detection ─────────────────────────────────────────────────────

def check_one(row_idx: int, ref: str, code: str, url: str) -> dict:
    base = {"row": row_idx, "ref": ref, "code": code, "url": url}

    if not url or code in SKIP_CODES:
        return {**base, "sold": False, "reason": "skip"}

    status, final_url, text = http_check(url)

    if status is None:
        return {**base, "sold": False, "reason": text}

    if status == 404:
        return {**base, "sold": True, "reason": "404"}

    if code in HOMEPAGE_REDIRECT_CODES:
        if is_homepage(url, final_url):
            return {**base, "sold": True, "reason": "redirect_homepage"}
        return {**base, "sold": False, "reason": f"ok_{status}"}

    if code == "CPA":
        if any(x in text for x in ("aucun résultat", "aucun resultat", "aucun bien", "no result")):
            return {**base, "sold": True, "reason": "aucun_resultat"}
        return {**base, "sold": False, "reason": f"ok_{status}"}

    if code == "CPSB":
        if any(x in text for x in ("page non trouvée", "page non trouvee", "not found", "introuvable", "404")):
            return {**base, "sold": True, "reason": "page_non_trouvee"}
        return {**base, "sold": False, "reason": f"ok_{status}"}

    if code in ("MI", "Mi"):
        if any(x in text for x in ("le bien cherché", "n'existe pas", "nexiste pas", "bien cherche", "propertynotfound", "does not exist")):
            return {**base, "sold": True, "reason": "bien_nexiste_pas"}
        return {**base, "sold": False, "reason": f"ok_{status}"}

    if code == "MIH":
        if status == 404 or any(x in text for x in ("introuvable", "not found", "page non trouvée", "cette page")):
            return {**base, "sold": True, "reason": "404_or_notfound"}
        return {**base, "sold": False, "reason": f"ok_{status}"}

    if code == "ISB":
        if status == 404 or any(x in text for x in ("introuvable", "bien introuvable", "not found", "page introuvable")):
            return {**base, "sold": True, "reason": "404_isb"}
        return {**base, "sold": False, "reason": f"ok_{status}"}

    if code == "GCI":
        return {**base, "sold": None, "reason": "gci_defer"}

    # Default: LV, WE, and others
    if status == 404:
        return {**base, "sold": True, "reason": "404"}
    return {**base, "sold": False, "reason": f"ok_{status}"}


# ─── GCI: sold page ────────────────────────────────────────────────────────────

def check_gci_sold_page(gci_rows: list[dict]) -> list[dict]:
    log.info("  GCI: chargement page biens-vendus...")
    status, _, text_raw = http_check("https://gelas-immobilier.com/fr/biens-vendus", timeout=20)
    if not text_raw or status != 200:
        log.warning(f"  Page GCI vendus inaccessible (status={status})")
        return [{**r, "sold": None, "reason": "gci_page_unavailable"} for r in gci_rows]

    sold_hrefs = set(re.findall(r'href=["\']([^"\']+propriete[^"\']+)["\']', text_raw))
    sold_hrefs |= set(re.findall(r'href=["\']([^"\']+/vente[^"\']+)["\']', text_raw))

    results = []
    for r in gci_rows:
        our_path = urlparse(r["url"].lower()).path.rstrip("/")
        matched = any(
            our_path in href.lower() or urlparse(href).path.rstrip("/") == our_path
            for href in sold_hrefs
        )
        if matched:
            results.append({**r, "sold": True, "reason": "gci_sold_page"})
        else:
            s, _, t = http_check(r["url"])
            if s == 404:
                results.append({**r, "sold": True, "reason": "404"})
            else:
                results.append({**r, "sold": False, "reason": f"ok_{s}"})
    return results


# ─── Sovimo: sold biens page ───────────────────────────────────────────────────

def _extract_sovimo_prop_id(url: str) -> str | None:
    """Extract numeric property ID from last path segment of a Sovimo vente URL."""
    last = url.rstrip("/").split("/")[-1]
    m = re.match(r"^(\d+)-", last)
    if m and int(m.group(1)) > 10:  # exclude pagination numbers (1-10)
        return m.group(1)
    return None


def check_sovimo_sold_page(sov_rows: list[dict]) -> list[dict]:
    log.info("  SOV: chargement pages biens-vendus...")
    sold_ids: set[str] = set()
    for page in range(1, 200):
        status, _, text = http_check(
            f"https://www.sovimo-immobilier-confolens.fr/biens-vendus/{page}", timeout=15
        )
        if status != 200:
            break
        hrefs = re.findall(r'href="(/vente/[^"]+)"', text)
        page_ids = {
            pid for h in hrefs if (pid := _extract_sovimo_prop_id(h))
        }
        if not page_ids:
            break
        sold_ids |= page_ids

    log.info(f"  SOV: {len(sold_ids)} biens vendus trouvés sur la page")

    results = []
    for r in sov_rows:
        prop_id = _extract_sovimo_prop_id(r["url"])
        if prop_id and prop_id in sold_ids:
            results.append({**r, "sold": True, "reason": "sovimo_sold_page"})
        else:
            results.append({**r, "sold": False, "reason": f"sov_id_not_found({prop_id})"})
    return results


# ─── Google Sheets ─────────────────────────────────────────────────────────────

def get_service_account_creds() -> Credentials:
    """Load service account from env var (CI) or local file (dev)."""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        info = json.loads(sa_json)
        return Credentials.from_service_account_info(
            info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
    # Fallback: local file (dev only)
    local_file = os.path.join(os.path.dirname(__file__), "..", "apimo-integration", "google_service_account.json")
    return Credentials.from_service_account_file(
        local_file, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )


def bold_rows_in_sheet(sold_rows: list[int]) -> None:
    if not sold_rows:
        return
    creds = get_service_account_creds()
    service = build("sheets", "v4", credentials=creds)

    meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_id = next(
        s["properties"]["sheetId"]
        for s in meta["sheets"]
        if s["properties"]["title"] == SHEET_NAME
    )

    requests_body = [
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                },
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat.bold",
            }
        }
        for row_num in sold_rows
    ]

    for i in range(0, len(requests_body), 200):
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": requests_body[i : i + 200]},
        ).execute()
        if i + 200 < len(requests_body):
            time.sleep(1)

    log.info(f"  {len(sold_rows)} ligne(s) mise(s) en gras dans le sheet")


# ─── Email ─────────────────────────────────────────────────────────────────────

def send_sold_email(sold: list[dict], unsure: list[dict]) -> None:
    today = datetime.now().strftime("%d/%m/%Y")
    lines = [
        f"Goedemorgen {config.RECIPIENT_NAME},",
        "",
        f"Dagelijkse sold-check — {today}",
        "",
        f"Er werden {len(sold)} verkochte panden gedetecteerd in Apimo.",
        "",
    ]

    if sold:
        lines.append("─" * 50)
        lines.append(f"VERKOCHT ({len(sold)}) — in vet gemarkeerd in het sheet:")
        lines.append("─" * 50)
        for r in sorted(sold, key=lambda x: x["code"]):
            lines.append(f"• [{r['code']}] {r['ref']} — {r['url']}")
        lines.append("")

    if unsure:
        lines.append("─" * 50)
        lines.append(f"TE CONTROLEREN ({len(unsure)}) — niet zeker:")
        lines.append("─" * 50)
        for r in sorted(unsure, key=lambda x: x["code"]):
            lines.append(f"• [{r['code']}] {r['ref']} — {r['url']}")
        lines.append("")

    lines += [
        "─" * 50,
        "",
        "Groetjes,",
        config.SENDER_NAME,
    ]

    body = "\n".join(lines)
    subject = f"Sold-check Apimo — {len(sold)} verkocht — {today}"
    recipients = [e.strip() for e in config.RECIPIENT_EMAILS.split(",") if e.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{config.SENDER_NAME} <{config.SMTP_USER}>"
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
        smtp.sendmail(config.SMTP_USER, recipients, msg.as_string())

    log.info(f"Email sold-check envoyé: {len(sold)} vendus, {len(unsure)} incertains")


# ─── Main ──────────────────────────────────────────────────────────────────────

def run_sold_check() -> None:
    log.info("=== Sold check démarré ===")

    # Load sheet
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if sa_json:
        gc = gspread.service_account_from_dict(json.loads(sa_json))
    else:
        local_file = os.path.join(os.path.dirname(__file__), "..", "apimo-integration", "google_service_account.json")
        gc = gspread.service_account(filename=local_file)

    ws = gc.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    rows = ws.get_all_values()

    en_cours = []
    for i, r in enumerate(rows[1:], start=2):
        if len(r) > 7 and r[7].strip() == "En cours":
            code = r[0].split()[0] if r[0] else ""
            en_cours.append({
                "row": i,
                "ref":  r[0].strip(),
                "code": code,
                "url":  r[5].strip() if len(r) > 5 else "",
            })

    log.info(f"{len(en_cours)} biens 'En cours' à vérifier")

    gci_rows   = [r for r in en_cours if r["code"] == "GCI"]
    sov_rows   = [r for r in en_cours if r["code"] == "SOV"]
    http_rows  = [r for r in en_cours if r["code"] not in ("GCI", "SOV")]

    # Phase 1: HTTP checks
    results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=30) as pool:
        futures = {
            pool.submit(check_one, r["row"], r["ref"], r["code"], r["url"]): r
            for r in http_rows
        }
        for fut in as_completed(futures):
            res = fut.result()
            results[res["row"]] = res

    # Phase 2: GCI sold page
    if gci_rows:
        for r in check_gci_sold_page(gci_rows):
            results[r["row"]] = r

    # Phase 3: Sovimo sold page
    if sov_rows:
        for r in check_sovimo_sold_page(sov_rows):
            results[r["row"]] = r

    all_results = list(results.values())
    sold   = [r for r in all_results if r.get("sold") is True]
    unsure = [r for r in all_results if r.get("sold") is None]

    log.info(f"Résultats: {len(sold)} vendus, {len(unsure)} incertains, {len(all_results) - len(sold) - len(unsure)} en ligne")

    # Bold sold rows in sheet
    if sold:
        bold_rows_in_sheet([r["row"] for r in sold])

    # Send email if anything found
    if sold or unsure:
        try:
            send_sold_email(sold, unsure)
        except Exception as e:
            log.error(f"Echec envoi email sold-check: {e}")

    log.info("=== Sold check terminé ===")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    run_sold_check()
