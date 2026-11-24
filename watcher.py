"""Börsen-Watcher: schickt Push-Nachrichten per ntfy bei wichtigen Meldungen
und schreibt eine Übersichtsseite (docs/).

Quellen: SEC EDGAR (große US-Techwerte, IPOs), Fed-Pressemitteilungen,
Wirtschaftstermine (BLS, BEA, FOMC), Kurse (Finnhub).
Kein Handelssignal, keine Anlageberatung.
"""

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import explain

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "seen.json"
DATA_FILE = BASE / "docs" / "data.json"
NY = ZoneInfo("America/New_York")
WIEN = ZoneInfo("Europe/Vienna")
NTFY_URL = "https://ntfy.sh"
FED_FEED = "https://www.federalreserve.gov/feeds/press_all.xml"
SEC_FEED = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
    "&type=&dateb=&owner=include&count=40&output=atom"
)
IPO_FEED = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=424B4&count=40&output=atom"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_ICS = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
FINNHUB_QUOTE = "https://finnhub.io/api/v1/quote"
DISCLAIMER = "Zum Nachprüfen, kein Handelssignal."
MAX_SEEN = 3000
MAX_HISTORY = 150
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
        what_label = ", ".join(labels) if labels else FORMS_DE.get(form, form)
        what, typical = explain.sec(form, nums)
        items.append({
            "src": f"sec:{name}",
            "id": "sec:" + (e.findtext(f"{ATOM}id") or ""),
            "kind": "earnings" if "2.02" in nums else "company",
            "title": f"{'📊 ' if '2.02' in nums else ''}{name}: {what_label}",
            "what": what,
            "typical": typical,
            "symbol": spec.get("ticker"),
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
        items.append({
            "src": "ipo",
            "id": "ipo:" + (e.findtext(f"{ATOM}id") or title),
            "kind": "ipo",
            "title": f"🚀 Börsengang/Platzierung: {m.group(1) if m else title}",
            "what": explain.IPO[0],
            "typical": explain.IPO[1],
            "symbol": None,
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
        what, typical = explain.fed(title)
        items.append({
            "src": "fed",
            "id": "fed:" + (it.findtext("guid") or link),
            "kind": "fed",
            "title": "🏦 Fed: " + title,
            "what": what,
            "typical": typical,
            "symbol": cfg.get("market_symbol", "SPY"),
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


def fetch_agenda(cfg, contact, first, last):
    """Wichtige Termine zwischen first und last (jeweils inklusive)."""
    headers = {"User-Agent": f"BoersenWatcher {contact}"}
    events = []
    for name, url in (("BLS", BLS_ICS), ("BEA", BEA_ICS)):
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        for dt, title in ics_events(r.text):
            if first <= dt.date() <= last and matches(title, cfg["agenda_keywords"]):
                events.append((dt, title))
    for d in cfg.get("fomc_decision_dates", []):
        day = date.fromisoformat(d)
        if first <= day <= last:
            events.append((datetime(day.year, day.month, day.day, 14, 0, tzinfo=NY), "FOMC"))
    out = []
    for dt, title in sorted(events):
        label, what, typical = explain.agenda(title)
        out.append({"ny": dt.isoformat(), "local": dt.astimezone(WIEN).isoformat(),
                    "label": label, "original": title, "what": what, "typical": typical})
    return out


# ---------- Kurse ----------

def fetch_prices(cfg, key):
    """Aktueller Kurs und Tagesveränderung je Symbol (Finnhub, kostenlos)."""
    prices = {}
    for sym, name in cfg.get("watchlist", {}).items():
        r = requests.get(FINNHUB_QUOTE, params={"symbol": sym}, headers={"X-Finnhub-Token": key}, timeout=20)
        r.raise_for_status()
        q = r.json()
        if q.get("c"):
            prices[sym] = {"name": name, "price": q["c"], "change_pct": q.get("dp"), "prev_close": q.get("pc")}
    return prices


# ---------- Ablauf ----------

def main():
    force = os.environ.get("FORCE_RUN") == "true"
    dry = os.environ.get("DRY_RUN") == "true"
    topic = os.environ.get("NTFY_TOPIC", "")
    contact = os.environ.get("SEC_CONTACT_EMAIL", "")
    finnhub = os.environ.get("FINNHUB_KEY", "")
    cfg = load_json(CONFIG_FILE, {})
    now = datetime.now(NY)

    if cfg.get("paused"):
        print("Pausiert (config.json: paused = true).")
        return

    state = load_json(STATE_FILE, {})
    for k, v in (("ids", []), ("failures", {}), ("alerted", {}), ("init_sources", [])):
        state.setdefault(k, v)
    state["failures"].pop("SEC EDGAR", None)  # alter Name aus Version 1
    state["alerted"].pop("SEC EDGAR", None)
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

    data = load_json(DATA_FILE, {"items": []})

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

    # Termine: Vorschau 14 Tage für die Seite, Tagesübersicht als Push
    agenda = guarded("Wirtschaftstermine",
                     lambda: fetch_agenda(cfg, contact, now.date(), now.date() + timedelta(days=14)))
    if agenda is not None:
        data["agenda"] = agenda
        if agenda_due:
            today = [e for e in agenda if e["ny"][:10] == now.date().isoformat()]
            if today:
                lines = []
                for e in today:
                    t_ny, t_local = e["ny"][11:16], e["local"][11:16]
                    lines.append(f"{t_ny} NY / {t_local} bei dir: {e['label']}\n→ {e['typical']}")
                send("📅 Termine heute", "\n\n".join(lines))
            state["agenda_sent"] = now.date().isoformat()

    prices = {}
    if finnhub and (force or is_open):
        prices = guarded("Kurse (Finnhub)", lambda: fetch_prices(cfg, finnhub)) or {}
        if prices:
            data["prices"] = prices
            data["prices_updated"] = now.isoformat()

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

        limit = cfg.get("max_per_run", 5)
        for item in to_send:
            sym = item.get("symbol")
            item["time"] = now.isoformat()
            item["price_at"] = prices.get(sym, {}).get("price") if sym else None
            data["items"].insert(0, {k: v for k, v in item.items() if k != "src"})
        for item in to_send[:limit]:
            send(item["title"], f"{DISCLAIMER}\n\nWas ist das? {item['what']}\n\nTypisch: {item['typical']}",
                 item["link"])
        if len(to_send) > limit:
            print(f"{len(to_send) - limit} weitere Treffer nicht gesendet (Limit), stehen aber auf der Seite.")
        if force and not to_send:
            send("🧪 Test", "Watcher läuft. Gerade keine neuen Meldungen.")
        print(f"{len(to_send)} neue Treffer.")
        state["ids"] = (state["ids"] + [i["id"] for i in new])[-MAX_SEEN:]

    data["items"] = data["items"][:MAX_HISTORY]
    data["updated"] = now.isoformat()
    data["sources"] = {k: ("ok" if v == 0 else f"{v}x Fehler") for k, v in state["failures"].items()}

    if dry:
        print(json.dumps({k: data[k] for k in ("agenda", "sources") if k in data}, ensure_ascii=False)[:1500])
    else:
        STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        DATA_FILE.parent.mkdir(exist_ok=True)
        DATA_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
