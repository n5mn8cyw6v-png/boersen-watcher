"""Börsen-Watcher mit Demo-Portfolio.

- Eilmeldungen sofort: starke Bewegungen deiner Positionen, wichtige News zu deinen
  Werten, Aussagen von Big Playern (Trump, Musk, Buffett …) mit Marktbezug,
  Quartalszahlen deiner Firmen, Depot-Meldungen großer Investoren, Fed-Zinsentscheid.
- Stündliches Markt-Update während der US-Börsenzeit.
- Verschlüsselte Übersichtsseite (docs/), Passwort nur als GitHub-Secret.

Nur Information zum Nachprüfen. Kein Handelssignal, keine Anlageberatung.
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
import translate
import investors
import news
import portfolio
import store

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
DATA_FILE = BASE / "docs" / "data.enc"
LOCAL_PORTFOLIO = BASE / "portfolio.local.json"
NY = ZoneInfo("America/New_York")
WIEN = ZoneInfo("Europe/Vienna")
NTFY_URL = "https://ntfy.sh"
FED_FEED = "https://www.federalreserve.gov/feeds/press_all.xml"
SEC_FEED = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
            "&type=&dateb=&owner=include&count=40&output=atom")
IPO_FEED = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=424B4&count=40&output=atom"
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_ICS = "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
DISCLAIMER = "Zum Nachprüfen, kein Handelssignal."
MAX_SEEN = 5000
MAX_HISTORY = 200
ATOM = "{http://www.w3.org/2005/Atom}"
ITEMS_DE = {"2.02": "Quartalszahlen", "2.01": "Übernahme/Verkauf abgeschlossen", "1.01": "Wichtiger Vertrag",
            "5.02": "Vorstandswechsel", "7.01": "Mitteilung an Investoren", "8.01": "Sonstige Meldung"}
FORMS_DE = {"4": "Insider-Transaktion", "10-Q": "Quartalsbericht", "10-K": "Jahresbericht",
            "13F-HR": "Depot-Meldung (13F)"}


def de_num(x, digits=2):
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def pct(x):
    return "–" if x is None else f"{'+' if x > 0 else ''}{de_num(x)} %"


def matches(text, keywords):
    return any(re.search(r"\b" + re.escape(k) + r"\b", text, re.IGNORECASE) for k in keywords)


def notify(topic, title, message, link=None, tags=None, priority=None):
    payload = {"topic": topic, "title": title, "message": message}
    if link:
        payload["click"] = link
    if tags:
        payload["tags"] = tags
    if priority:
        payload["priority"] = priority
    requests.post(NTFY_URL, json=payload, timeout=20).raise_for_status()


# ---------- SEC & Fed ----------

def sec_get(url, contact):
    headers = {"User-Agent": f"BoersenWatcher {contact}", "Accept-Encoding": "gzip, deflate"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    time.sleep(1)  # SEC: Anfragen begrenzen
    return ET.fromstring(r.content)


def fetch_company(name, spec, cfg, contact, held):
    root = sec_get(SEC_FEED.format(cik=spec["cik"]), contact)
    only_items = set(cfg.get("sec_8k_items_only", [])) if name != "Nvidia" else None
    out = []
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
        what, typical = explain.sec(form, nums)
        sym = spec.get("ticker")
        is_held = sym in held
        earnings = "2.02" in nums
        investor = form == "13F-HR"
        out.append({
            "src": f"sec:{name}", "id": "sec:" + (e.findtext(f"{ATOM}id") or ""),
            "kind": "investor" if investor else ("earnings" if earnings else "company"),
            "title": f"{'📊 ' if earnings else '🧠 ' if investor else ''}{name}: "
                     f"{', '.join(labels) if labels else FORMS_DE.get(form, form)}",
            "what": what, "typical": typical, "symbol": sym,
            "affected": [sym] if is_held else [],
            "urgent": is_held and (earnings or form == "8-K"),
            "source": "SEC EDGAR", "link": e.find(f"{ATOM}link").get("href"),
        })
    return out


def fetch_ipos(contact):
    root = sec_get(IPO_FEED, contact)
    out = []
    for e in root.findall(f"{ATOM}entry"):
        title = (e.findtext(f"{ATOM}title") or "").strip()
        m = re.match(r"424B4 - (.+?) \(\d+\)", title)
        out.append({
            "src": "ipo", "id": "ipo:" + (e.findtext(f"{ATOM}id") or title), "kind": "ipo",
            "title": f"🚀 Börsengang/Platzierung: {m.group(1) if m else title}",
            "what": explain.IPO[0], "typical": explain.IPO[1], "symbol": None, "affected": [],
            "urgent": False, "source": "SEC EDGAR", "link": e.find(f"{ATOM}link").get("href"),
        })
    return out


def fetch_fed(cfg):
    r = requests.get(FED_FEED, headers={"User-Agent": "BoersenWatcher"}, timeout=30)
    r.raise_for_status()
    out = []
    for it in ET.fromstring(r.content).iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not matches(f"{title} {desc}", cfg["keywords"]):
            continue
        what, typical = explain.fed(title)
        out.append({
            "src": "fed", "id": "fed:" + (it.findtext("guid") or link), "kind": "fed",
            "title": "🏦 Fed: " + title, "what": what, "typical": typical,
            "symbol": "SPY", "affected": [], "urgent": "FOMC statement" in title,
            "source": "Federal Reserve", "link": link,
            "en": title, "prefix": "🏦 Fed: ",
        })
    return out


def fetch_news(pf, cfg, key, today):
    out = []

    def add(n, src, held_symbol=None):
        c = news.classify(n, pf, cfg, held_symbol)
        if not c["relevant"]:
            return
        head = re.sub(r"\s+-\s+(Reuters|CNBC|Bloomberg)$", "", n.get("headline", "").strip())
        summary = (n.get("summary") or "").strip()
        if summary.lower().startswith(head.lower()[:40]):
            summary = ""  # Zusammenfassung wiederholt nur die Überschrift
        text = f"{head} {summary}"
        prefix = "🗣️ " if c["players"] else "📰 "
        out.append({
            "src": src, "id": f"fh:{n['id']}", "kind": "player" if c["players"] else "news",
            "title": prefix + head,
            "what": summary[:300],
            "typical": explain.news(text), "symbol": (c["affected"] or [None])[0] or ("SPY" if c["macro"] else None),
            "affected": c["affected"], "players": c["players"], "urgent": c["urgent"],
            "source": n.get("source", ""), "link": n.get("url", ""),
            "en": head, "prefix": prefix,
        })

    for n in news.general(key):
        add(n, "news:general")
    for p in pf["positions"]:
        if p.get("news"):
            for n in news.company(p["proxy"], key, today):
                add(n, f"news:{p['proxy']}", p["proxy"])
    # Doppelte Artikel (gleiche ID aus mehreren Abfragen) entfernen
    uniq = {}
    for i in out:
        if i["id"] in uniq:
            uniq[i["id"]]["affected"] = sorted(set(uniq[i["id"]]["affected"]) | set(i["affected"]))
        else:
            uniq[i["id"]] = i
    return list(uniq.values())


# ---------- Termine ----------

def ics_events(text):
    text = re.sub(r"\r?\n[ \t]", "", text)
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
    headers = {"User-Agent": f"BoersenWatcher {contact}"}
    events = []
    for url in (BLS_ICS, BEA_ICS):
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
                    "label": label, "what": what, "typical": typical})
    return out


# ---------- Ablauf ----------

def load_portfolio():
    raw = os.environ.get("PORTFOLIO_JSON")
    if raw:
        return json.loads(raw)
    if LOCAL_PORTFOLIO.exists():
        return json.loads(LOCAL_PORTFOLIO.read_text(encoding="utf-8"))
    return None


def main():
    env = os.environ.get
    force, dry = env("FORCE_RUN") == "true", env("DRY_RUN") == "true"
    topic, contact, fh_key, password = env("NTFY_TOPIC", ""), env("SEC_CONTACT_EMAIL", ""), env("FINNHUB_KEY", ""), env("PAGE_PASSWORD", "")
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    pf = load_portfolio()
    now = datetime.now(NY)
    today = now.date()

    if cfg.get("paused"):
        print("Pausiert (config.json: paused = true).")
        return
    weekday = now.weekday() < 5
    active = weekday and 8 <= now.hour < 18
    session = weekday and (now.hour, now.minute) >= (9, 30) and now.hour < 17
    if not (force or active):
        print("Außerhalb der Zeiten, nichts zu tun.")
        return
    missing = [n for n, v in (("NTFY_TOPIC", topic or dry), ("SEC_CONTACT_EMAIL", contact),
                              ("FINNHUB_KEY", fh_key), ("PAGE_PASSWORD", password or dry), ("PORTFOLIO_JSON", pf)) if not v]
    if missing:
        sys.exit("Fehlt: " + ", ".join(missing))

    db = {"state": {}, "page": {}}
    if DATA_FILE.exists() and password:
        db = store.decrypt(json.loads(DATA_FILE.read_text()), password)
    state, page = db["state"], db["page"]
    for k, v in (("ids", []), ("failures", {}), ("alerted", {}), ("init_sources", []), ("alert_keys", []),
                 ("pending", []), ("history", [])):
        state.setdefault(k, v)
    page.setdefault("items", [])

    def send(title, message, link=None, tags=None, priority=None):
        if dry:
            print(f"[PUSH] {title}\n       " + message.replace("\n", "\n       ") + (f"\n       {link}" if link else ""))
        else:
            notify(topic, title, message, link, tags, priority)

    def guarded(name, fn):
        try:
            res = fn()
            state["failures"][name] = 0
            state["alerted"][name] = False
            return res
        except Exception as exc:
            n = state["failures"].get(name, 0) + 1
            state["failures"][name] = n
            print(f"Fehler bei {name} ({n}x): {exc}")
            if n >= cfg.get("failure_alert_after", 3) and not state["alerted"].get(name):
                send("⚠️ Watcher-Fehler", f"{name} schlägt seit {n} Läufen fehl: {exc}", tags=["warning"])
                state["alerted"][name] = True
            return None

    def once(key):
        """True, wenn dieser Alarm noch nicht gesendet wurde (und merkt ihn sich)."""
        if key in state["alert_keys"]:
            return False
        state["alert_keys"] = (state["alert_keys"] + [key])[-500:]
        return True

    urgent = []  # (title, message, link)

    # 1) Termine
    agenda = guarded("Wirtschaftstermine", lambda: fetch_agenda(cfg, contact, today, today + timedelta(days=14)))
    if agenda is not None:
        page["agenda"] = agenda
        if cfg.get("agenda") and state.get("agenda_sent") != today.isoformat():
            todays = [e for e in agenda if e["ny"][:10] == today.isoformat()]
            if todays:
                send("📅 Termine heute", "\n\n".join(
                    f"{e['local'][11:16]} Uhr bei dir: {e['label']}\n→ {e['typical']}" for e in todays))
            state["agenda_sent"] = today.isoformat()

    # 2) Kurse & Demo-Portfolio
    proxies = {p["proxy"] for p in pf["positions"]}
    quotes = guarded("Kurse (Finnhub)", lambda: news.quotes(proxies | set(cfg["watchlist"]), fh_key)) or {}
    fx = guarded("EZB-Wechselkurs", portfolio.eurusd)
    ev = None
    if quotes and fx:
        base = state.setdefault("base", {"fx": fx, "proxy": {}, "date": today.isoformat()})
        for s in proxies:
            if s in quotes and s not in base["proxy"]:
                base["proxy"][s] = quotes[s]["c"]
        ev = portfolio.evaluate(pf, quotes, fx, base)
        page["portfolio"] = ev
        page["risk"] = portfolio.risk_check(ev)
        page["prices"] = {s: {"name": n, "price": quotes[s]["c"], "change_pct": quotes[s].get("dp")}
                          for s, n in cfg["watchlist"].items() if s in quotes}
        hist = [h for h in state["history"] if h["d"] != today.isoformat()] + [{"d": today.isoformat(), "v": ev["total"]}]
        state["history"] = hist[-800:]
        page["history"] = state["history"]
        peak = max(h["v"] for h in hist)

        def fresh(sym):
            t = quotes.get(sym, {}).get("t")
            return t and datetime.fromtimestamp(t, NY).date() == today

        if session:
            th = cfg["move_alert_pct"]
            for r in ev["positions"]:
                d = r["day_pct"]
                if d is None or not fresh(r["proxy"]) or abs(d) < th.get(r["type"], 5):
                    continue
                if once(f"move:{today}:{r['proxy']}:{'up' if d > 0 else 'down'}"):
                    urgent.append((f"{'🔻' if d < 0 else '🔺'} {r['short']} {pct(d)} heute",
                                   f"Deine Position {r['name']} bewegt sich stark ({pct(d)}, Gewicht {de_num(r['weight'], 1)} %).\n"
                                   "Prüfen: Gibt es eine Nachricht dazu? Passt die Position noch zu deinem Ziel "
                                   "(langfristig, mittleres Risiko)? Kurzfristige Schwankungen sind bei Einzelaktien normal.",
                                   None))
            if fresh("SPY") and ev["day_pct"] <= -cfg["portfolio_day_alert_pct"] and once(f"pfday:{today}"):
                urgent.append((f"🔻 Demo-Depot {pct(ev['day_pct'])} heute",
                               f"Das Depot hat heute {de_num(ev['day_abs'])} € verloren. "
                               "Meist zieht der Gesamtmarkt mit – Termine und Nachrichten prüfen.", None))
        dd = (peak - ev["total"]) / peak * 100 if peak else 0
        if dd >= cfg["drawdown_alert_pct"] and once(f"dd:{today.isocalendar()[0]}-{today.isocalendar()[1]}"):
            urgent.append((f"📉 Depot {de_num(dd, 1)} % unter Höchststand",
                           "Größerer Rückgang seit dem Höchststand. Für langfristiges Anlegen sind solche Phasen normal; "
                           "prüfen, ob Risiko und Aufteilung noch zum Ziel passen.", None))

    # 3) Was die Profis tun: Großinvestoren (13F) und Analysten
    inv_state = state.setdefault("investors", {})
    for name, cik in cfg.get("investors", {}).items():
        def check(name=name, cik=cik):
            latest = investors.latest_filings(cik, contact, n=1)
            if not latest:
                return
            old = inv_state.get(name)
            if old and old.get("acc") == latest[0]["acc"]:
                return
            summary = investors.analyze(name, cik, contact)
            summary["overlap"] = investors.overlap(summary, pf)
            inv_state[name] = summary
            if old:  # beim allerersten Mal nur merken, nicht melden
                mine = "".join(f"\nDein Depot: {o['position']} – {investors.KIND_DE[o['kind']]}"
                               + (f" ({o['change_pct']:+d} %)" if o.get("change_pct") else "")
                               for o in summary["overlap"] if o["kind"] != "largest")
                urgent.append((f"🧠 {name}: neue Depot-Meldung",
                               f"Stand {summary['period']} (gemeldet {summary['filed']}).\n"
                               f"{investors.short_text(summary)}{mine}\n\n"
                               f"Das sind Entscheidungen von {name.split('(')[-1].rstrip(')')}, keine Empfehlung. "
                               "Die Daten sind bis zu 45 Tage alt.",
                               f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=13F-HR"))
        guarded(f"13F {name}", check)
    page["investors"] = [inv_state[n] for n in cfg.get("investors", {}) if n in inv_state]

    tr = translate.Translator(state, today.isoformat())

    # Analysten: nur Meldungen der letzten 5 Tage, bei jedem Lauf neu
    def load_analysts():
        out = []
        for p in pf["positions"]:
            if p["type"] == "stock":
                acts = news.analyst_actions(p, fh_key, today)
                for a in acts:
                    a["de"] = tr(a["headline"])
                    a["terms"] = translate.terms(a["headline"], 2)
                out.append({"position": p["short"], "symbol": p["proxy"], "actions": acts})
        return out
    an = guarded("Analysten (Finnhub)", load_analysts)
    if an is not None:
        page["analysts"] = an

    # 4) Meldungen sammeln
    held = proxies
    found, ok = [], set()

    def collect(src, label, fn):
        res = guarded(label, fn)
        if res is not None:
            found.extend(res)
            ok.add(src)

    for name, spec in cfg.get("sec_companies", {}).items():
        collect(f"sec:{name}", f"SEC {name}", lambda n=name, s=spec: fetch_company(n, s, cfg, contact, held))
    if cfg.get("ipos"):
        collect("ipo", "SEC IPOs", lambda: fetch_ipos(contact))
    collect("fed", "Federal Reserve", lambda: fetch_fed(cfg))
    news_items = guarded("Nachrichten (Finnhub)", lambda: fetch_news(pf, cfg, fh_key, today))
    if news_items is not None:
        found.extend(news_items)
        ok |= {"news:general"} | {f"news:{p['proxy']}" for p in pf["positions"] if p.get("news")}

    seen = set(state["ids"])
    new = [i for i in found if i["id"] not in seen]
    fresh_src = ok - set(state["init_sources"])
    if fresh_src:
        print(f"Neue Quellen eingerichtet ({len(fresh_src)}), Altbestand nicht gesendet.")
        state["init_sources"] += sorted(fresh_src)
    to_report = [i for i in new if i["src"] not in fresh_src]
    state["ids"] = (state["ids"] + [i["id"] for i in new])[-MAX_SEEN:]

    prices = {s: q["c"] for s, q in quotes.items()}
    if prices:
        page["quotes"] = prices
    # Ins Deutsche übersetzen: wichtige Meldungen zuerst (Tageskontingent ist begrenzt)
    for i in sorted(to_report, key=lambda i: (not i["urgent"], not i["affected"])):
        if not i.get("en"):
            continue
        de = tr(i["en"])
        if de:
            i["title"] = i["prefix"] + de
        if i["kind"] in ("news", "player") and i["what"] and (i["urgent"] or i["affected"]):
            what_de = tr(i["what"][:300])
            if what_de:
                i["what"] = what_de
        i["terms"] = translate.terms(f"{i['en']} {i.get('what', '')}")
    for i in to_report:
        i.pop("prefix", None)
        i["time"] = now.isoformat()
        i["price_at"] = prices.get(i.get("symbol"))
        page["items"].insert(0, {k: v for k, v in i.items() if k != "src"})
        if i["urgent"]:
            aff = f"Betrifft dein Depot: {', '.join(i['affected'])}\n\n" if i["affected"] else ""
            who = f"Big Player: {', '.join(i.get('players', []))}\n" if i.get("players") else ""
            urgent.append(("🚨 " + i["title"],
                           f"{who}{aff}" + (f"{i['what'][:220]}\n\n" if i["what"] else "")
                           + (("Begriffe:\n" + "\n".join("• " + t for t in i["terms"]) + "\n\n") if i.get("terms") else "")
                           + f"Typisch: {i['typical']}\nQuelle: {i['source']}", i["link"]))
        else:
            state["pending"].append({"t": i["title"], "a": i["affected"]})
    page["items"] = page["items"][:MAX_HISTORY]

    # 5) Eilmeldungen senden
    hour_key = f"{today} {now.hour}"
    sent_this_hour = state.get("urgent_sent", {}).get(hour_key, 0)
    limit = max(0, cfg.get("max_urgent_per_hour", 4) - sent_this_hour)
    state["urgent_sent"] = {hour_key: sent_this_hour + min(limit, len(urgent))}
    for title, msg, link in urgent[:limit]:
        send(title, f"{DISCLAIMER}\n\n{msg}", link, tags=["rotating_light"], priority=4)
    for title, _, _ in urgent[limit:]:
        state["pending"].append({"t": title, "a": []})
    state["pending"] = state["pending"][-60:]

    # 6) Stündliches Markt-Update
    if (force or session) and state.get("last_hourly") != hour_key and ev:
        q = quotes
        movers = sorted([r for r in ev["positions"] if r["day_pct"] is not None], key=lambda r: r["day_pct"])
        lines = [
            f"Demo-Depot: {de_num(ev['total'])} € ({pct(ev['day_pct'])} heute | {pct(ev['pl_pct'])} gesamt)",
            f"Markt: S&P 500 {pct(q.get('SPY', {}).get('dp'))} · Nasdaq {pct(q.get('QQQ', {}).get('dp'))}",
        ]
        if movers:
            lines.append(f"Stärkste: {movers[-1]['short']} {pct(movers[-1]['day_pct'])} · "
                         f"Schwächste: {movers[0]['short']} {pct(movers[0]['day_pct'])}")
        pend = sorted(state["pending"], key=lambda p: -len(p["a"]))
        if pend:
            lines.append("\nNeu seit dem letzten Update:")
            lines += [f"• {p['t'][:110]}" + (f" ({', '.join(p['a'])})" if p["a"] else "") for p in pend[:3]]
            if len(pend) > 3:
                lines.append(f"+ {len(pend) - 3} weitere auf der Seite")
        else:
            lines.append("\nKeine neuen Meldungen seit dem letzten Update.")
        send(f"📈 Markt-Update {now.astimezone(WIEN):%H:%M}", "\n".join(lines), cfg.get("page_url"))
        state["pending"] = []
        state["last_hourly"] = hour_key

    page["updated"] = now.isoformat()
    page["sources"] = {k: ("ok" if v == 0 else f"{v}x Fehler") for k, v in state["failures"].items()}
    print(f"{len(to_report)} neue Meldungen, {len(urgent)} Eilmeldungen.")

    if dry:
        out = Path(env("DRY_OUT", "")) if env("DRY_OUT") else None
        if out:
            out.write_text(json.dumps(page, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        DATA_FILE.parent.mkdir(exist_ok=True)
        DATA_FILE.write_text(json.dumps(store.encrypt(db, password)), encoding="utf-8")


if __name__ == "__main__":
    main()
