"""Wohnung-Radar: sucht neue 2-Zimmer-Wohnungen und schickt sie an Telegram."""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
STATE_FILE = ROOT / "seen.json"
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "de-DE,de;q=0.9",
}

# Muster, an denen wir Anzeigen-Links auf den Ergebnisseiten erkennen
LINK_PATTERNS = {
    "kleinanzeigen": re.compile(r"^/s-anzeige/[^?#]+/(\d+)-\d+-\d+"),
    "wg-gesucht": re.compile(r"/wohnungen-in-[^/?#]+\.(\d+)\.html"),
    "immowelt": re.compile(r"/expose/([a-z0-9]+)", re.I),
}


# ---------------------------------------------------------------- Hilfsfunktionen
def money(text):
    """'1.234,50 €' -> 1234.5"""
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d{1,2}))?", text)
    if not m:
        return None
    return float(m.group(1).replace(".", "") + ("." + m.group(2) if m.group(2) else ""))


def find_amount(text, *labels):
    for label in labels:
        m = re.search(label + r"[^\d€]{0,25}(\d[\d.]*(?:,\d{1,2})?)\s*(?:€|EUR|Euro)", text, re.I)
        if m:
            return money(m.group(1))
    return None


def find_number(text, pattern):
    m = re.search(pattern, text, re.I)
    return float(m.group(1).replace(",", ".")) if m else None


def find_date(text):
    m = re.search(r"(?:verfügbar|frei|bezugsfrei|einzug)[^\d\n]{0,20}?(ab\s*)?\n?\s*(sofort|\d{1,2}\.\d{1,2}\.\d{2,4}|"
                  r"(?:januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|dezember)\s*\d{4})",
                  text, re.I)
    return m.group(2) if m else None


def detect_kitchen(text):
    t = text.lower()
    if re.search(r"(ohne|keine)\s+(einbau)?küche|küche\s+(muss|ist)\s+[^.]{0,30}(selbst|mitgebracht|gestellt)", t):
        return "no"
    abl = re.search(r"(?:einbau)?küche[^.\n]{0,60}ablöse[^\d\n]{0,15}(\d[\d.]*)?|ablöse[^.\n]{0,40}küche", t)
    if abl:
        return ("abloese", money(abl.group(1)) if abl.lastindex and abl.group(1) else None)
    if re.search(r"einbauküche|\bebk\b|küchenzeile|pantryküche|kochnische|mit küche|küche vorhanden|"
                 r"küche ist vorhanden|inkl\.?\s*küche|voll ausgestattete küche|möblierte küche", t):
        return "yes"
    return "unknown"


def rating(eur_m2):
    r = CONFIG["rating"]
    if eur_m2 is None:
        return "–"
    if eur_m2 < r["suspicious_below"]:
        return "⚠️ auffällig billig – Vorsicht Betrug"
    if eur_m2 <= r["cheap_up_to"]:
        return "🟢 günstig"
    if eur_m2 <= r["fair_up_to"]:
        return "🟡 fair"
    return "🔴 teuer"


def fmt(x, dec=0):
    if x is None:
        return "?"
    s = f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return s


# ---------------------------------------------------------------- Abruf
session = requests.Session()
session.headers.update(HEADERS)


def get(url):
    r = session.get(url, timeout=25)
    if r.status_code in (403, 429) or "captcha" in r.text.lower()[:5000]:
        raise PermissionError(f"blockiert ({r.status_code})")
    r.raise_for_status()
    return r.text


def list_ads(search):
    page = get(search["url"])
    soup = BeautifulSoup(page, "html.parser")
    pattern = LINK_PATTERNS[search["site"]]
    found = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = pattern.search(href)
        if m and m.group(1) not in found:
            found[m.group(1)] = urljoin(search["url"], href.split("#")[0])
    return [(f"{search['site']}:{ad_id}", url) for ad_id, url in found.items()]


def parse_detail(site, url):
    soup = BeautifulSoup(get(url), "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    text = soup.get_text("\n", strip=True)
    title = (soup.find("h1") or soup.find("title"))
    title = title.get_text(" ", strip=True) if title else "Wohnung"

    ad = {"site": site, "url": url, "title": title[:90], "text": text}
    ad["kalt"] = find_amount(text, r"Kaltmiete", r"Nettokaltmiete", r"\bMiete(?! inkl)")
    if site == "kleinanzeigen":
        price = soup.select_one("#viewad-price")
        if price:
            ad["kalt"] = money(price.get_text())
        loc = soup.select_one("#viewad-locality")
        ad["ort"] = loc.get_text(" ", strip=True) if loc else None
    else:
        m = re.search(r"\b(6\d{4})\s+Mannheim[^\n]{0,40}", text)
        ad["ort"] = m.group(0) if m else None

    ad["warm"] = find_amount(text, r"Warmmiete", r"Gesamtmiete", r"Miete inkl")
    ad["nk"] = find_amount(text, r"Nebenkosten", r"Betriebskosten")
    ad["heiz"] = find_amount(text, r"Heizkosten")
    ad["kaution"] = find_amount(text, r"Kaution", r"Mietsicherheit")
    ad["m2"] = find_number(text, r"(?:Wohnfläche|Größe)[^\d]{0,15}(\d+(?:,\d+)?)\s*m²") \
        or find_number(text, r"(\d+(?:,\d+)?)\s*m²")
    ad["zimmer"] = find_number(text, r"Zimmer[^\d]{0,10}(\d(?:,5)?)\b") \
        or find_number(text, r"(\d(?:,5)?)\s*-?\s*Zi(?:mmer)?\b")
    ad["frei"] = find_date(text)
    ad["kitchen"] = detect_kitchen(text)

    if not ad["warm"] and ad["kalt"] and ad["nk"]:
        ad["warm"] = ad["kalt"] + ad["nk"] + (ad["heiz"] or 0)
        ad["warm_est"] = False
    return ad


def matches(ad):
    f = CONFIG["filter"]
    if re.search(r"\b(suche|tausch\w*)\b", ad["title"], re.I):
        return False
    if ad["zimmer"] and not (f["min_rooms"] <= ad["zimmer"] <= f["max_rooms"]):
        return False
    total = ad["warm"] or ((ad["kalt"] or 0) + f["assumed_nk_if_unknown"])
    if total and total > f["max_warm"]:
        return False
    return True


def message(ad):
    eur_m2 = ad["kalt"] / ad["m2"] if ad["kalt"] and ad["m2"] else None
    kitchen = ad["kitchen"]
    if kitchen == "yes":
        k = "✅ vorhanden"
    elif kitchen == "no":
        k = "❌ keine"
    elif isinstance(kitchen, tuple):
        k = "💶 gegen Ablöse" + (f" ({fmt(kitchen[1])} €)" if kitchen[1] else "")
    else:
        k = "❓ unklar – nachfragen"

    warm = fmt(ad["warm"]) + " €" if ad["warm"] else f"~{fmt((ad['kalt'] or 0) + CONFIG['filter']['assumed_nk_if_unknown'])} € (geschätzt)"
    lines = [
        f"🏠 <b>{html.escape(ad['title'])}</b>",
        f"📍 {html.escape(ad['ort'] or 'Mannheim')} · {fmt(ad['zimmer'], 1).replace(',0', '')} Zi · {fmt(ad['m2'])} m²",
        f"Kalt: {fmt(ad['kalt'])} € | NK: {fmt(ad['nk'])} €" + (f" | Heiz: {fmt(ad['heiz'])} €" if ad["heiz"] else ""),
        f"Warm: {warm}",
        f"€/m² kalt: {fmt(eur_m2, 2)} → {rating(eur_m2)}",
        f"Küche: {k}",
    ]
    if ad["kaution"]:
        lines.append(f"Kaution: {fmt(ad['kaution'])} €")
    if ad["frei"]:
        lines.append(f"Frei ab: {html.escape(ad['frei'])}")
    lines.append(f'🔗 <a href="{html.escape(ad["url"])}">{ad["site"]}</a>')
    return "\n".join(lines)


def send(text):
    if DRY_RUN or not TOKEN:
        print("---\n" + text)
        return
    for _ in range(3):
        r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=20)
        if r.status_code == 429:
            time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            continue
        if not r.ok:
            print("Telegram-Fehler:", r.text, file=sys.stderr)
        break
    time.sleep(1.2)


# ---------------------------------------------------------------- Ablauf
def main():
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"seen": {}, "warned": {}}
    seen, warned = state["seen"], state.setdefault("warned", {})
    now = datetime.now(timezone.utc)
    sent = 0

    for search in CONFIG["searches"]:
        if "HIER_" in search["url"]:
            continue
        try:
            ads = list_ads(search)
            print(f"{search['name']}: {len(ads)} Anzeigen auf der Liste")
            if not ads:
                raise PermissionError("keine Anzeigen gefunden – Seite geändert oder blockiert?")
        except Exception as e:
            print(f"{search['name']}: {e}", file=sys.stderr)
            last = warned.get(search["name"])
            if not last or (now - datetime.fromisoformat(last)).total_seconds() > 86400:
                send(f"⚠️ {search['name']}: {html.escape(str(e))}")
                warned[search["name"]] = now.isoformat()
            continue

        for key, url in ads[: CONFIG["max_details_per_run"]]:
            if key in seen:
                continue
            try:
                ad = parse_detail(search["site"], url)
            except Exception as e:
                print(f"  {url}: {e}", file=sys.stderr)
                continue
            seen[key] = now.isoformat()
            if matches(ad):
                send(message(ad))
                sent += 1
            time.sleep(2)

    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False))
    print(f"Fertig, {sent} neue Anzeigen geschickt.")


if __name__ == "__main__":
    main()
