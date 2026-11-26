"""
Sold check quotidien — vérifie le statut des biens 'En cours' dans le Google Sheet Apimo.
- Lecture seule du sheet (rien n'est modifié).
- Envoie un email récapitulatif avec les liens ; Marine traite les vendus elle-même.

Règles par partenaire (HTTP uniquement, sans Playwright) :
  AC, DA, AD, AUDR, ABF, BFS             → redirect vers homepage = vendu
  SOI, SOV (Sovimo), TCY (PY), GCI       → pages "biens vendus" (ces sites gardent les vendus en ligne)
  CPA                                    → texte "aucun résultat"
  CPSB                                   → texte "page non trouvée"
  MI, Mi                                 → texte "le bien cherché n'existe pas"
  MIH, ISB, LV, WE                       → 404 ou page "not found"
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
HOMEPAGE_REDIRECT_CODES = {"AC", "DA", "AD", "AUDR", "ABF", "BFS"}


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

    if status in (404, 410):
        return {**base, "sold": True, "reason": str(status)}

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

    # Default: LV, WE, and others
    if status == 404:
        return {**base, "sold": True, "reason": "404"}
    return {**base, "sold": False, "reason": f"ok_{status}"}


# ─── Sold pages (sites that keep sold listings online) ───────────────────────

def fetch_text(url: str, timeout: int = 30, tries: int = 3) -> str | None:
    """GET with retries (partner sites are sometimes slow from GitHub runners)."""
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            if r.status_code == 200:
                return r.text
            if r.status_code in (404, 410):
                return None
        except requests.RequestException:
            pass
        time.sleep(3 * (attempt + 1))
    return None


def _slug_id(url: str) -> str | None:
    """Sovimo / PY: /vente/<ville>/<type>/4944-slug/ → '4944'."""
    m = re.match(r"^(\d+)-", url.rstrip("/").split("/")[-1])
    return m.group(1) if m and int(m.group(1)) > 10 else None   # > 10 skips pagination links


def _gci_id(url: str) -> str | None:
    """Gélas: /fr/propriete/vente+maison+ville+slug+87171219 → '87171219'."""
    m = re.search(r"\+(\d{6,})/?$", urlparse(url).path)
    return m.group(1) if m else None


def collect_sold_ids(page_tpl: str, extract, max_pages: int = 100) -> set[str] | None:
    """IDs on every page of a 'biens vendus' listing. None = first page unreachable."""
    sold: set[str] = set()
    for page in range(1, max_pages + 1):
        text = fetch_text(page_tpl.format(page=page))
        if text is None:
            return sold if page > 1 else None
        ids = extract(text)
        if not ids or ids <= sold:      # empty or repeated last page
            break
        sold |= ids
    return sold


SOLD_PAGES = {
    # codes: (sold page template, ids found on that page, id of our listing URL)
    ("SOI", "SOV"): ("https://www.sovimo-immobilier-confolens.fr/biens-vendus/{page}",
                     lambda t: {i for h in re.findall(r'href="([^"]*/vente/[^"]+)"', t) if (i := _slug_id(h))},
                     _slug_id),
    ("TCY",):       ("http://www.py-immobilier.fr/biens-vendus/{page}",
                     lambda t: {i for h in re.findall(r'href="([^"]*/vente/[^"]+)"', t) if (i := _slug_id(h))},
                     _slug_id),
    ("GCI",):       ("https://gelas-immobilier.com/fr/biens-vendus?page={page}",
                     lambda t: set(re.findall(r'data-property-id="(\d+)"', t)),
                     _gci_id),
}
SOLD_PAGE_CODES = {c for codes in SOLD_PAGES for c in codes}


def check_sold_page(codes: tuple, rows: list[dict]) -> list[dict]:
    tpl, extract, own_id = SOLD_PAGES[codes]
    sold_ids = collect_sold_ids(tpl, extract)
    log.info(f"  {'/'.join(codes)}: {('page inaccessible' if sold_ids is None else f'{len(sold_ids)} vendus sur le site')}")
    out = []
    for r in rows:
        pid = own_id(r["url"])
        if sold_ids is None:
            out.append({**r, "sold": False, "reason": "__unchecked__sold_page_down"})
        elif pid and pid in sold_ids:
            out.append({**r, "sold": True, "reason": "sold_page"})
        elif pid is None:
            # URL without listing ID (search link): only a dead page tells us something
            status, final_url, _ = http_check(r["url"])
            gone = status in (404, 410) or (final_url and is_homepage(r["url"], final_url) and "not-found" in final_url)
            out.append({**r, "sold": bool(gone), "reason": "404" if gone else "no_id"})
        else:
            out.append({**r, "sold": False, "reason": "online"})
    return out


# ─── Email ─────────────────────────────────────────────────────────────────────

def send_sold_email(sold: list[dict], unsure: list[dict], unchecked: dict[str, int]) -> None:
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
        lines.append(f"VERKOCHT ({len(sold)}):")
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

    if unchecked:
        lines.append("─" * 50)
        lines.append("NIET GECONTROLEERD (site onbereikbaar vandaag):")
        lines.append("─" * 50)
        for code, n in sorted(unchecked.items()):
            lines.append(f"• [{code}] {n} panden")
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

    log.info(f"Email sold-check envoyé: {len(sold)} vendus, {len(unsure)} incertains, non vérifiés {unchecked}")


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

    http_rows = [r for r in en_cours if r["code"] not in SOLD_PAGE_CODES]

    results: dict[int, dict] = {}
    # Phase 1 (before the per-listing burst, which can get us rate-limited): partners that keep sold listings online → their "biens vendus" pages
    for codes in SOLD_PAGES:
        rows_ = [r for r in en_cours if r["code"] in codes]
        if rows_:
            for r in check_sold_page(codes, rows_):
                results[r["row"]] = r

    # Phase 2: one HTTP request per listing (8 workers: partner sites block bursts)
    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in as_completed([pool.submit(check_one, r["row"], r["ref"], r["code"], r["url"]) for r in http_rows]):
            res = fut.result()
            results[res["row"]] = res

    all_results = list(results.values())
    sold   = [r for r in all_results if r.get("sold") is True]
    unsure = [r for r in all_results if r.get("sold") is None]

    # Per-partner summary; a partner mostly unreachable today is flagged in the mail
    by_code: dict[str, dict[str, int]] = {}
    for r in all_results:
        st = "vendu" if r["sold"] else ("non_vérifié" if str(r["reason"]).startswith("__") else
                                        "skip" if r["reason"] == "skip" else "en_ligne")
        by_code.setdefault(r["code"], {}).setdefault(st, 0)
        by_code[r["code"]][st] += 1
    for code, c in sorted(by_code.items()):
        log.info(f"  [{code}] {c}")
    unchecked = {code: c["non_vérifié"] for code, c in by_code.items()
                 if c.get("non_vérifié", 0) >= max(3, 0.3 * sum(c.values()))}

    log.info(f"Résultats: {len(sold)} vendus, {len(unsure)} incertains, non vérifiés {unchecked}")

    if sold or unsure or unchecked:
        send_sold_email(sold, unsure, unchecked)   # a failed mail must fail the run (visible on GitHub)

    log.info("=== Sold check terminé ===")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s", datefmt="%H:%M:%S")
    run_sold_check()
