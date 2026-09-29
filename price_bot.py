#!/usr/bin/env python3
"""
Bot prezzi Mac -> WhatsApp (via CallMeBot).

Uso:
    python price_bot.py --dry-run     # stampa il messaggio, non lo invia
    python price_bot.py               # raccoglie i prezzi e invia su WhatsApp

Variabili d'ambiente (solo per l'invio):
    CALLMEBOT_PHONE   es. +393331234567
    CALLMEBOT_APIKEY  la chiave ricevuta da CallMeBot
"""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
HISTORY_FILE = BASE / "history.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.6",
}

STORE_LABELS = {
    "amazon": "Amazon",
    "mediaworld": "MediaWorld",
    "unieuro": "Unieuro",
    "euronics": "Euronics",
}

# Selettori CSS di riserva, usati solo se JSON-LD e meta tag non bastano.
# Se uno store cambia layout, e' qui che si interviene.
SELECTORS = {
    "amazon": [
        "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-offscreen",
        "#corePrice_feature_div .a-offscreen",
        ".priceToPay .a-offscreen",
        "#apex_desktop .a-offscreen",
        "#newBuyBoxPrice",
        "#price_inside_buybox",
        "#tp_price_block_total_price_ww .a-offscreen",
        "span.a-price:not(.a-text-price) .a-offscreen",
        ".a-price .a-offscreen",
        "#priceblock_ourprice",
    ],
    "mediaworld": [
        "[data-test='mms-select-price']",
        "[data-test='branded-price-whole-value']",
    ],
    "unieuro": [
        ".product-price .price",
        "[class*='price'] [class*='current']",
    ],
    "euronics": [
        ".price-container .price",
        "[class*='product-price']",
    ],
}

MIN_PRICE, MAX_PRICE = 400.0, 8000.0  # scarta valori palesemente sbagliati


# ---------------------------------------------------------------- parsing ---
def parse_price(raw):
    """Converte '1.299,00 EUR', '1299.00', 1299 ecc. in float. None se non valido."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
    else:
        s = re.sub(r"[^\d,.]", "", str(raw)).strip(".,")
        if not s:
            return None
        if "," in s and "." in s:
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            s = s.replace(",", ".")
        elif "." in s:
            head, _, tail = s.rpartition(".")
            if len(tail) == 3 and head.replace(".", "").isdigit():
                s = s.replace(".", "")  # 1.299 -> 1299
        try:
            v = float(s)
        except ValueError:
            return None
    return v if MIN_PRICE <= v <= MAX_PRICE else None


def _walk(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for v in node.values():
            if isinstance(v, (dict, list)):
                yield from _walk(v)


def price_from_jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text())
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if any(x in ("Offer", "AggregateOffer") for x in types):
                for key in ("price", "lowPrice"):
                    p = parse_price(node.get(key))
                    if p:
                        return p
    return None


def price_from_meta(soup):
    candidates = [
        {"itemprop": "price"},
        {"property": "product:price:amount"},
        {"property": "og:price:amount"},
    ]
    for attrs in candidates:
        tag = soup.find("meta", attrs=attrs)
        if tag and tag.get("content"):
            p = parse_price(tag["content"])
            if p:
                return p
    return None


def price_from_selectors(soup, store):
    for sel in SELECTORS.get(store, []):
        for el in soup.select(sel)[:5]:
            p = parse_price(el.get("content") or el.get_text(" ", strip=True))
            if p:
                return p
    return None


def price_from_regex(html, store):
    """Ultima spiaggia: cerca il prezzo nel JSON incorporato nella pagina."""
    if store != "amazon":
        return None
    patterns = (
        r'"priceAmount"\s*:\s*"?([\d.,]+)',
        r'"displayPrice"\s*:\s*"[^"\d]*([\d.,]+)',
        r'"buyingPrice"\s*:\s*"?([\d.,]+)',
    )
    for pat in patterns:
        for m in re.finditer(pat, html):
            p = parse_price(m.group(1))
            if p:
                return p
    return None


def extract_price(html, store):
    soup = BeautifulSoup(html, "html.parser")
    return (
        price_from_jsonld(soup)
        or price_from_meta(soup)
        or price_from_selectors(soup, store)
        or price_from_regex(html, store)
    )


def looks_blocked(html):
    """True solo se la pagina somiglia a una schermata anti-bot (piccola + parole chiave)."""
    low = html.lower()
    keys = (
        "captcha",
        "robot check",
        "access denied",
        "are you a human",
        "verifica di essere",
        "inserisci i caratteri",
        "digita i caratteri",
        "enter the characters you see",
        "non sei un robot",
        "unusual traffic",
        "request blocked",
        "pardon our interruption",
    )
    return len(html) < 40000 and any(k in low for k in keys)


def page_hint(html):
    """Titolo della pagina, utile per capire cosa e' arrivato davvero."""
    try:
        t = BeautifulSoup(html, "html.parser").title
        return (t.get_text(strip=True) if t else "")[:35]
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- fetching ---
def fetch_requests(url):
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return r.text


def fetch_playwright(url):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            locale="it-IT",
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3500)
        html = page.content()
        browser.close()
    return html


def get_price(store, url):
    """Ritorna (prezzo | None, nota_errore | None)."""
    note = None
    # 1) richiesta semplice (veloce)
    try:
        html = fetch_requests(url)
        p = extract_price(html, store)  # si prova SEMPRE a leggere il prezzo
        if p:
            return p, None
        hint = page_hint(html)
        print(f"[{store}] requests: nessun prezzo, len={len(html)}, titolo='{hint}'")
        note = f"bloccato: {hint}" if looks_blocked(html) else f"prezzo non trovato: {hint}"
    except Exception as e:  # noqa: BLE001
        note = str(e)[:40]
        print(f"[{store}] requests errore: {note}")

    # 2) browser vero (se Playwright e' installato)
    try:
        html = fetch_playwright(url)
        p = extract_price(html, store)
        if p:
            return p, None
        hint = page_hint(html)
        print(f"[{store}] playwright: nessun prezzo, len={len(html)}, titolo='{hint}'")
        return None, (f"bloccato: {hint}" if looks_blocked(html) else f"prezzo non trovato: {hint}")
    except ImportError:
        return None, note
    except Exception as e:  # noqa: BLE001
        print(f"[{store}] playwright errore: {str(e)[:80]}")
        return None, note or str(e)[:40]


# ----------------------------------------------------------------- storico ---
def load_history():
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_history(hist):
    HISTORY_FILE.write_text(
        json.dumps(hist, indent=1, ensure_ascii=False), encoding="utf-8"
    )


def previous_price(hist, key, today):
    for entry in reversed(hist.get(key, [])):
        if entry["date"] != today:
            return entry["price"]
    return None


def record(hist, key, today, price):
    entries = [e for e in hist.get(key, []) if e["date"] != today]
    entries.append({"date": today, "price": price})
    hist[key] = entries[-90:]


# ---------------------------------------------------------------- messaggio ---
def eur(v):
    s = f"{v:,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def delta_text(now, before):
    if before is None:
        return ""
    diff = now - before
    if abs(diff) < 0.5:
        return " (=)"
    arrow = "🔻" if diff < 0 else "🔺"
    return f" ({arrow}{eur(abs(diff))})"


def build_blocks(config, hist, today):
    blocks = []
    for prod in config["products"]:
        name = prod["name"]
        lines = [f"*{name}*"]
        found = []
        for store, url in prod.get("urls", {}).items():
            if not str(url).startswith("http"):
                continue  # URL non ancora compilato
            label = STORE_LABELS.get(store, store)
            price, err = get_price(store, url)
            if price:
                key = f"{name}|{store}"
                lines.append(f"• {label}: {eur(price)}{delta_text(price, previous_price(hist, key, today))}")
                record(hist, key, today, price)
                found.append((price, label))
            else:
                lines.append(f"• {label}: n/d ({err})")
            time.sleep(random.uniform(2, 5))
        if found:
            best_price, best_store = min(found)
            lines.append(f"✅ Migliore: {best_store} {eur(best_price)}")
            target = prod.get("target_price")
            if target and best_price <= target:
                lines.append(f"🔔 Sotto il tuo target di {eur(target)}!")
        blocks.append("\n".join(lines))
    return blocks


# -------------------------------------------------------------------- invio ---
def send_whatsapp(text):
    phone = os.environ.get("CALLMEBOT_PHONE")
    key = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not key:
        sys.exit("Mancano CALLMEBOT_PHONE e/o CALLMEBOT_APIKEY.")
    r = requests.get(
        "https://api.callmebot.com/whatsapp.php",
        params={"phone": phone, "text": text, "apikey": key},
        timeout=60,
    )
    print("CallMeBot:", r.status_code, r.text[:150].replace("\n", " "))
    if r.status_code != 200:
        sys.exit(1)


def chunk(blocks, header, limit=1400):
    """Raggruppa i blocchi in messaggi sotto il limite di lunghezza."""
    messages, current = [], header
    for b in blocks:
        if len(current) + len(b) + 2 > limit and current != header:
            messages.append(current)
            current = ""
        current = (current + "\n\n" + b) if current else b
    if current:
        messages.append(current)
    return messages


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="non inviare, stampa soltanto")
    args = ap.parse_args()

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    today = date.today().isoformat()
    hist = load_history()

    blocks = build_blocks(config, hist, today)
    if not blocks:
        sys.exit("Nessun prodotto in config.json.")

    header = f"☀️ *Prezzi Mac* – {date.today().strftime('%d/%m/%Y')}"
    messages = chunk(blocks, header)
    if not messages[0].startswith(header):
        messages[0] = header + "\n\n" + messages[0]

    save_history(hist)

    for m in messages:
        if args.dry_run:
            print(m, "\n" + "-" * 30)
        else:
            send_whatsapp(m)
            time.sleep(3)


if __name__ == "__main__":
    main()
        
