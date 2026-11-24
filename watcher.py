"""Börsen-Watcher: schickt Push-Nachrichten per ntfy bei wichtigen Meldungen.

Quellen (Version 1): SEC EDGAR (Nvidia) und Fed-Pressemitteilungen.
Kein Handelssignal, keine Anlageberatung.
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "seen.json"
NTFY_URL = "https://ntfy.sh"
FED_FEED = "https://www.federalreserve.gov/feeds/press_all.xml"
SEC_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    "&type=&dateb=&owner=include&count=40&output=atom"
)
DISCLAIMER = "Zum Nachprüfen, kein Handelssignal."
MAX_SEEN = 2000
ATOM = "{http://www.w3.org/2005/Atom}"


def market_open(now=None):
    now = now or datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def notify(topic, title, message, link=None, tags=None):
    payload = {"topic": topic, "title": title, "message": message}
    if link:
        payload["click"] = link
    if tags:
        payload["tags"] = tags
    r = requests.post(NTFY_URL, json=payload, timeout=20)
    r.raise_for_status()


def fetch_sec(cfg, contact):
    """SEC EDGAR Atom-Feeds je Firma. Max. 1 Anfrage pro Sekunde."""
    headers = {"User-Agent": f"BoersenWatcher {contact}", "Accept-Encoding": "gzip, deflate"}
    forms = set(cfg["sec_forms"])
    items = []
    for name, cik in cfg["sec_ciks"].items():
        r = requests.get(SEC_FEED.format(cik=cik), headers=headers, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for e in root.findall(f"{ATOM}entry"):
            cat = e.find(f"{ATOM}category")
            form = cat.get("term") if cat is not None else ""
            if form not in forms:
                continue
            link = e.find(f"{ATOM}link").get("href")
            items.append({
                "id": "sec:" + (e.findtext(f"{ATOM}id") or link),
                "title": f"{name}: Form {form}",
                "text": (e.findtext(f"{ATOM}title") or "").strip(),
                "source": "SEC EDGAR",
                "link": link,
            })
        time.sleep(1)
    return items


def fetch_fed(cfg):
    r = requests.get(FED_FEED, headers={"User-Agent": "BoersenWatcher"}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not matches(f"{title} {desc}", cfg["keywords"]):
            continue
        items.append({
            "id": "fed:" + (it.findtext("guid") or link),
            "title": "Fed: " + title,
            "text": desc[:200],
            "source": "Federal Reserve",
            "link": link,
        })
    return items


def matches(text, keywords):
    for kw in keywords:
        if re.search(r"\b" + re.escape(kw) + r"\b", text, re.IGNORECASE):
            return True
    return False


def main():
    force = os.environ.get("FORCE_RUN") == "true"
    dry = os.environ.get("DRY_RUN") == "true"
    topic = os.environ.get("NTFY_TOPIC", "")
    contact = os.environ.get("SEC_CONTACT_EMAIL", "")
    cfg = load_json(CONFIG_FILE, {})

    if cfg.get("paused"):
        print("Pausiert (config.json: paused = true).")
        return
    if not force and not market_open():
        print("Börse geschlossen, nichts zu tun.")
        return
    if not dry and not topic:
        sys.exit("NTFY_TOPIC fehlt.")
    if not contact:
        sys.exit("SEC_CONTACT_EMAIL fehlt.")

    def send(title, message, link=None, tags=None):
        if dry:
            print(f"[DRY] {title}\n      {message}\n      {link}")
        else:
            notify(topic, title, message, link, tags)

    state = load_json(STATE_FILE, {"ids": [], "failures": {}, "alerted": {}})
    first_run = not state["ids"]
    seen = set(state["ids"])

    sources = {
        "SEC EDGAR": lambda: fetch_sec(cfg, contact),
        "Federal Reserve": lambda: fetch_fed(cfg),
    }
    found = []
    for name, fn in sources.items():
        try:
            found += fn()
            state["failures"][name] = 0
            state["alerted"][name] = False
        except Exception as exc:  # Quelle ausgefallen
            n = state["failures"].get(name, 0) + 1
            state["failures"][name] = n
            print(f"Fehler bei {name} ({n}x): {exc}")
            if n >= cfg.get("failure_alert_after", 3) and not state["alerted"].get(name):
                send("⚠️ Watcher-Fehler", f"{name} schlägt seit {n} Läufen fehl: {exc}", tags=["warning"])
                state["alerted"][name] = True

    new = [i for i in found if i["id"] not in seen]
    if first_run:
        # Beim allerersten Lauf alte Meldungen nur merken, nicht senden.
        print(f"Erster Lauf: {len(new)} bestehende Meldungen gemerkt.")
        send("✅ Börsen-Watcher aktiv",
             f"Eingerichtet. {len(new)} ältere Meldungen übersprungen. Ab jetzt kommen nur neue.")
    else:
        for item in new[: cfg.get("max_per_run", 5)]:
            send(item["title"], f"{DISCLAIMER}\n{item['text']}\nQuelle: {item['source']}\n{item['link']}",
                 item["link"])
        if len(new) > cfg.get("max_per_run", 5):
            print(f"{len(new) - cfg['max_per_run']} weitere Treffer nicht gesendet (Limit).")
        if force and not new:
            send("🧪 Test", "Watcher läuft. Gerade keine neuen Meldungen.")
        print(f"{len(new)} neue Treffer.")

    ids = state["ids"] + [i["id"] for i in new]
    state["ids"] = ids[-MAX_SEEN:]
    if not dry:
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
