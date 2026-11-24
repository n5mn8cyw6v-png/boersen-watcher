"""Börsen-Watcher: schickt Push-Nachrichten per ntfy bei wichtigen Meldungen.

Quellen: SEC EDGAR (große US-Techwerte, IPOs), Fed-Pressemitteilungen,
Wirtschaftstermine (BLS, BEA, FOMC). Kein Handelssignal, keine Anlageberatung.
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "seen.json"
NY = ZoneInfo("America/New_York")
NTFY_URL = "https://ntfy.sh"
FED_FEED = "https://www.federalreserve.gov/feeds/press_all.xml"
SEC_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    "&type=&dateb=&owner=include&count=40&output=atom"
)
IPO_FEED = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=424B4&count=40&output=atom"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_ICS = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
DISCLAIMER = "Zum Nachprüfen, kein Handelssignal."
MAX_SEEN = 3000
ATOM = "{http://www.w3.org/2005/Atom}"
ITEMS_DE = {
    "2.02": "Quartalszahlen",
    "2.01": "Übernahme/Verkauf abgeschlossen",
    "1.01": "Wichtiger Vertrag",
    "5.02": "Vorstandswechsel",
    "7.01": "Mitteilung an Investoren",
    "8.01": "Sonstige Meldung",
}
FORMS_DE = {"4": "Insider-Transaktion", "10-Q": "Quartalsbericht", "10-K": "Jahresbericht"}
AGENDA_DE = {
    "Consumer Price Index": "Inflation (CPI)",
    "Producer Price Index": "Erzeugerpreise (PPI)",
    "Employment Situation": "Arbeitsmarktbericht",
    "Job Openings": "Offene Stellen (JOLTS)",
    "Personal Income and Outlays": "Konsum & PCE-Inflation",
    "Gross Domestic Product": "BIP",
    "Retail Sales": "Einzelhandelsumsätze",
    "Real Earnings": "Reallöhne",
}


def market_open(now):
    """8-18 Uhr New York: vorbörslich bis kurz nach Handelsschluss,
    damit Quartalszahlen nach Börsenschluss (meist ca. 16:05) noch erfasst werden."""
    if now.weekday() >= 5:
        return False
    return 8 <= now.hour < 18


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
    requests.post(NTFY_URL, json=payload, timeout=20).raise_for_status()


def matches(text, keywords):
    return any(re.search(r"\b" + re.escape(k) + r"\b", text, re.IGNORECASE) for k in keywords)


# ---------- SEC ----------

def sec_get(url, contact):
    headers = {"User-Agent": f"BoersenWatcher {contact}", "Accept-Encoding": "gzip, deflate"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    time.sleep(1)  # SEC: Anfragen begrenzen
    return ET.fromstring(r.content)


def fetch_company(name, spec, cfg, contact):
    root = sec_get(SEC_FEED.format(cik=spec["cik"]), contact)
    only_items = set(cfg.get("sec_8k_items_only", [])) if name != "Nvidia" else None
    items = []
    for e in root.findall(f"{ATOM}entry"):
        cat = e.find(f"{ATOM}category")
        form = cat.get("term") if cat is not None else ""
        if form not in spec["forms"]:
            continue
        content = e.find(f"{ATOM}content")
        desc = (content.findtext(f"{ATOM}items-desc") if content is not None else "") or ""
        nums = re.findall(r"\d\.\d\d", desc)
        if form == "8-K" and only_items is not None and not only_items & set(nums):
            continue
        labels = [ITEMS_DE[n] for n in nums if n in ITEMS_DE]
        what = ", ".join(labels) if labels else FORMS_DE.get(form, form)
        prefix = "📊 " if "2.02" in nums else ""
        items.append({
            "src": f"sec:{name}",
            "id": "sec:" + (e.findtext(f"{ATOM}id") or ""),
            "title": f"{prefix}{name}: {what}",
            "text": f"Form {form} ({desc or 'SEC-Meldung'})",
            "source": "SEC EDGAR",
            "link": e.find(f"{ATOM}link").get("href"),
        })
    return items


def fetch_ipos(contact):
    root = sec_get(IPO_FEED, contact)
    items = []
    for e in root.findall(f"{ATOM}entry"):
        title = (e.findtext(f"{ATOM}title") or "").strip()
        m = re.match(r"424B4 - (.+?) \(\d+\)", title)
        company = m.group(1) if m else title
        items.append({
            "src": "ipo",
            "id": "ipo:" + (e.findtext(f"{ATOM}id") or title),
            "title": f"🚀 Platzierung/IPO: {company}",
            "text": "Endgültiger Prospekt (424B4) eingereicht: Preis steht fest. Kann auch eine Folgeplatzierung sein.",
            "source": "SEC EDGAR",
            "link": e.find(f"{ATOM}link").get("href"),
        })
    return items


# ---------- Fed ----------

def fetch_fed(cfg):
    r = requests.get(FED_FEED, headers={"User-Agent": "BoersenWatcher"}, timeout=30)
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not matches(f"{title} {desc}", cfg["keywords"]):
            continue
        items.append({
            "src": "fed",
            "id": "fed:" + (it.findtext("guid") or link),
            "title": "🏦 Fed: " + title,
            "text": desc[:200],
            "source": "Federal Reserve",
            "link": link,
        })
    return items


# ---------- Wirtschaftstermine ----------

def ics_events(text):
    """Sehr einfacher iCal-Leser: liefert (Startzeit NY, Titel)."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # gefaltete Zeilen zusammenfügen
    for block in text.split("BEGIN:VEVENT")[1:]:
        start = re.search(r"^DTSTART([^:\n]*):(\d{8}T\d{6}Z?)", block, re.M)
        summary = re.search(r"^SUMMARY[^:\n]*:(.*)$", block, re.M)
        if not start or not summary:
            continue
        raw = start.group(2)
        dt = datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
        dt = dt.replace(tzinfo=timezone.utc if raw.endswith("Z") else NY).astimezone(NY)
        yield dt, summary.group(1).replace("\\,", ",").strip()


def fetch_agenda(cfg, contact, today):
    headers = {"User-Agent": f"BoersenWatcher {contact}"}
    events = []
    for name, url in (("BLS", BLS_ICS), ("BEA", BEA_ICS)):
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        for dt, title in ics_events(r.text):
            if dt.date() == today and matches(title, cfg["agenda_keywords"]):
                de = next((v for k, v in AGENDA_DE.items() if k.lower() in title.lower()), None)
                events.append((dt, f"{de}: {title}" if de else title))
    if today.isoformat() in cfg.get("fomc_decision_dates", []):
        events.append((datetime(today.year, today.month, today.day, 14, 0, tzinfo=NY),
                       "FOMC-Zinsentscheid, Pressekonferenz 14:30"))
    events.sort()
    return events


# ---------- Ablauf ----------

def main():
    force = os.environ.get("FORCE_RUN") == "true"
    dry = os.environ.get("DRY_RUN") == "true"
    topic = os.environ.get("NTFY_TOPIC", "")
    contact = os.environ.get("SEC_CONTACT_EMAIL", "")
    cfg = load_json(CONFIG_FILE, {})
    now = datetime.now(NY)

    if cfg.get("paused"):
        print("Pausiert (config.json: paused = true).")
        return

    state = load_json(STATE_FILE, {})
    state.setdefault("ids", [])
    state.setdefault("failures", {})
    state.setdefault("alerted", {})
    state.setdefault("init_sources", [])
    if state["ids"] and not state["init_sources"]:
        state["init_sources"] = ["sec:Nvidia", "fed"]  # Altbestand aus Version 1

    agenda_due = (cfg.get("agenda") and now.weekday() < 5 and now.hour >= 8
                  and state.get("agenda_sent") != now.date().isoformat())
    is_open = market_open(now)
    if not (force or is_open or agenda_due):
        print("Börse geschlossen, nichts zu tun.")
        return
    if not dry and not topic:
        sys.exit("NTFY_TOPIC fehlt.")
    if not contact:
        sys.exit("SEC_CONTACT_EMAIL fehlt.")

    def send(title, message, link=None, tags=None):
        if dry:
            print(f"[DRY] {title}\n      {message}\n      {link or ''}")
        else:
            notify(topic, title, message, link, tags)

    def guarded(name, fn):
        try:
            result = fn()
            state["failures"][name] = 0
            state["alerted"][name] = False
            return result
        except Exception as exc:
            n = state["failures"].get(name, 0) + 1
            state["failures"][name] = n
            print(f"Fehler bei {name} ({n}x): {exc}")
            if n >= cfg.get("failure_alert_after", 3) and not state["alerted"].get(name):
                send("⚠️ Watcher-Fehler", f"{name} schlägt seit {n} Läufen fehl: {exc}", tags=["warning"])
                state["alerted"][name] = True
            return None

    # Tagesvorschau Wirtschaftstermine (einmal pro Handelstag ab 8 Uhr New York)
    if agenda_due:
        events = guarded("Wirtschaftstermine", lambda: fetch_agenda(cfg, contact, now.date()))
        if events is not None:
            if events:
                lines = [f"{dt:%H:%M} NY ({dt.astimezone(ZoneInfo('Europe/Vienna')):%H:%M} bei dir): {t}"
                         for dt, t in events]
                send("📅 Termine heute", "\n".join(lines))
            state["agenda_sent"] = now.date().isoformat()

    if force or is_open:
        found, ok = [], set()

        def collect(src, label, fn):
            res = guarded(label, fn)
            if res is not None:
                found.extend(res)
                ok.add(src)

        for name, spec in cfg.get("sec_companies", {}).items():
            collect(f"sec:{name}", f"SEC {name}", lambda n=name, s=spec: fetch_company(n, s, cfg, contact))
        if cfg.get("ipos"):
            collect("ipo", "SEC IPOs", lambda: fetch_ipos(contact))
        collect("fed", "Federal Reserve", lambda: fetch_fed(cfg))

        seen = set(state["ids"])
        new = [i for i in found if i["id"] not in seen]
        # Neue Quellen: Altbestand nur merken, nicht senden.
        fresh = ok - set(state["init_sources"])
        to_send = [i for i in new if i["src"] not in fresh]
        if fresh:
            print(f"Neue Quellen eingerichtet: {', '.join(sorted(fresh))}")
            state["init_sources"] += sorted(fresh)
            if force:
                send("✅ Watcher erweitert", f"Neu dabei: {', '.join(sorted(fresh))}. Alte Meldungen übersprungen.")

        limit = cfg.get("max_per_run", 5)
        for item in to_send[:limit]:
            send(item["title"], f"{DISCLAIMER}\n{item['text']}\nQuelle: {item['source']}", item["link"])
        if len(to_send) > limit:
            print(f"{len(to_send) - limit} weitere Treffer nicht gesendet (Limit).")
        if force and not to_send and not fresh:
            send("🧪 Test", "Watcher läuft. Gerade keine neuen Meldungen.")
        print(f"{len(to_send)} neue Treffer.")
        state["ids"] = (state["ids"] + [i["id"] for i in new])[-MAX_SEEN:]

    if not dry:
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
