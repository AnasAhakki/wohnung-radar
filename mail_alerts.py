"""Liest Suchauftrag-Mails von ImmoScout24 und Immowelt aus Gmail (IMAP)."""
import email
import imaplib
import os
import re
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from urllib.parse import unquote

from bs4 import BeautifulSoup

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")

SOURCES = {
    "immoscout": {
        "sender": "immobilienscout24.de",
        "id": re.compile(r"immobilienscout24\.de/expose/(\d{6,})"),
        "url": "https://www.immobilienscout24.de/expose/{}",
    },
    "immowelt": {
        "sender": "immowelt.de",
        "id": re.compile(r"immowelt\.de/expose/([a-z0-9-]{6,})", re.I),
        "url": "https://www.immowelt.de/expose/{}",
    },
}


def enabled():
    return bool(GMAIL_USER and GMAIL_PASS)


def _html_of(msg):
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            raw = part.get_payload(decode=True) or b""
            return raw.decode(part.get_content_charset() or "utf-8", "replace")
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            raw = part.get_payload(decode=True) or b""
            return "<pre>" + raw.decode(part.get_content_charset() or "utf-8", "replace") + "</pre>"
    return ""


def _block_text(a):
    """Kleinsten umgebenden Block finden, der Preis und Fläche enthält."""
    node = a
    for _ in range(8):
        node = node.parent
        if node is None:
            break
        t = node.get_text("\n", strip=True)
        if "€" in t and ("m²" in t or "qm" in t.lower()):
            return t if len(t) < 1500 else t[:1500]
    return a.get_text("\n", strip=True)


def _hrefs(a):
    h = a.get("href", "")
    # Tracking-Links enthalten die echte Adresse oft URL-kodiert
    return h + " " + unquote(h) + " " + unquote(unquote(h))


def extract(site, html):
    src = SOURCES[site]
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        m = src["id"].search(_hrefs(a))
        if not m:
            continue
        ad_id = m.group(1)
        text = _block_text(a)
        if ad_id not in found or len(text) > len(found[ad_id]):
            found[ad_id] = text
    # Fallback: IDs ohne erkennbaren Block (reine Text-Mails)
    for ad_id in set(src["id"].findall(unquote(html))) - set(found):
        found[ad_id] = ""
    return [(f"{site}:{i}", src["url"].format(i), t) for i, t in found.items()]


def fetch(days=2):
    """Gibt [(key, url, text, site)] aus den Suchauftrag-Mails der letzten Tage zurück."""
    out = []
    box = imaplib.IMAP4_SSL("imap.gmail.com")
    try:
        box.login(GMAIL_USER, GMAIL_PASS)
        if box.select('"[Gmail]/All Mail"', readonly=True)[0] != "OK":
            box.select("INBOX", readonly=True)
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        for site, src in SOURCES.items():
            typ, data = box.search(None, f'(SINCE {since} FROM "{src["sender"]}")')
            if typ != "OK":
                continue
            for num in data[0].split()[-40:]:
                typ, msg_data = box.fetch(num, "(RFC822)")
                if typ != "OK":
                    continue
                msg = email.message_from_bytes(msg_data[0][1])
                subject = str(make_header(decode_header(msg.get("Subject", ""))))
                for key, url, text in extract(site, _html_of(msg)):
                    out.append((key, url, text, site, subject))
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return out
