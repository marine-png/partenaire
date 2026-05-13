from __future__ import annotations
import smtplib
import logging
from datetime import datetime
from email.mime.text import MIMEText

import config
from scrapers.base import Listing

log = logging.getLogger(__name__)


def format_price(price: int | None) -> str:
    if price is None:
        return "prijs onbekend"
    return f"{price:,.0f} €".replace(",", ".")


def build_body(all_changes: dict[str, dict]) -> str:
    today = datetime.now().strftime("%d/%m/%Y")
    lines = [
        f"Goedemorgen {config.RECIPIENT_NAME},",
        "",
        f"Dagelijkse update van de partnerwebsites — {today}",
        "",
    ]

    for site_name, changes in all_changes.items():
        new = changes.get("new", [])
        removed = changes.get("removed", [])
        price_changed = changes.get("price_changed", [])

        n_total = len(new) + len(removed) + len(price_changed)
        summary_parts = []
        if new:
            summary_parts.append(f"{len(new)} nieuw")
        if removed:
            summary_parts.append(f"{len(removed)} verwijderd")
        if price_changed:
            summary_parts.append(f"{len(price_changed)} prijswijziging")
        summary = " | ".join(summary_parts) if summary_parts else "geen wijzigingen"

        lines.append("─" * 50)
        lines.append(f"{site_name.upper()} — {summary}")
        lines.append("─" * 50)

        if new:
            lines.append(f"\nNIEUW TOEGEVOEGD ({len(new)}):")
            for listing in new:
                title = listing.title or "Onbekende titel"
                price = format_price(listing.price)
                lines.append(f"• {title} — {price}")
                lines.append(f"  {listing.url}")

        if removed:
            lines.append(f"\nVERWIJDERD ({len(removed)}):")
            for row in removed:
                title = row.get("title") or "Onbekende titel"
                price = format_price(row.get("price"))
                lines.append(f"• {title} — {price}")
                lines.append(f"  {row['listing_url']}")

        if price_changed:
            lines.append(f"\nPRIJSWIJZIGING ({len(price_changed)}):")
            for listing, old_price, new_price in price_changed:
                title = listing.title or "Onbekende titel"
                lines.append(
                    f"• {title} — {format_price(old_price)} → {format_price(new_price)}"
                )
                lines.append(f"  {listing.url}")

        lines.append("")

    lines += [
        "─" * 50,
        "",
        f"Groetjes,",
        config.SENDER_NAME,
    ]
    return "\n".join(lines)


def send_email(all_changes: dict[str, dict]) -> None:
    if not all_changes:
        return

    body = build_body(all_changes)

    total_changes = sum(
        len(c["new"]) + len(c["removed"]) + len(c["price_changed"])
        for c in all_changes.values()
    )
    n_sites = len(all_changes)
    subject = (
        f"Partnerwebsites — {total_changes} wijziging(en) bij {n_sites} agentschap(pen) "
        f"— {datetime.now().strftime('%d/%m/%Y')}"
    )

    recipients = [e.strip() for e in config.RECIPIENT_EMAILS.split(",") if e.strip()]

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{config.SENDER_NAME} <{config.SMTP_USER}>"
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            smtp.sendmail(config.SMTP_USER, recipients, msg.as_string())
        log.info(f"Email sent to {recipients} — {total_changes} changes")
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        raise
