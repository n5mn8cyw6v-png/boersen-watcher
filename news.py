"""Nachrichten über Finnhub (Reuters, CNBC, Bloomberg …) für Portfolio und Gesamtmarkt."""

import re
import time
from datetime import timedelta

import requests

FINNHUB = "https://finnhub.io/api/v1"
TRUSTED = {"reuters", "cnbc", "bloomberg", "marketwatch", "wsj", "barrons", "financial times", "yahoo", "ap"}
CLICKBAIT = re.compile(r"\?|\b(best|top \d+|should you|buy now|millionaire|forever|i'?m buying)\b", re.I)


def _get(path, key, **params):
    r = requests.get(f"{FINNHUB}/{path}", params=params, headers={"X-Finnhub-Token": key}, timeout=20)
    r.raise_for_status()
    time.sleep(0.25)  # unter 60 Anfragen/Minute bleiben
    return r.json()


def quotes(symbols, key):
    out = {}
    for s in sorted(set(symbols)):
        q = _get("quote", key, symbol=s)
        if q.get("c"):
            out[s] = q
    return out


ACTION = re.compile(r"\b(upgrade[sd]?|downgrade[sd]?|price target|fair value|initiat\w*|reiterat\w*|outperform|underperform|overweight|underweight|equal.weight|neutral rating|buy rating|sell rating|hold rating)\b", re.I)
UP = re.compile(r"\b(upgrade[sd]?|raises?|raised|lifts?|boosts?|hikes?|outperform|overweight|buy rating)\b", re.I)
DOWN = re.compile(r"\b(downgrade[sd]?|cuts?|lowers?|lowered|trims?|slashe[sd]|underperform|underweight|sell rating)\b", re.I)
ANALYST_SOURCES = TRUSTED | {"benzinga"}


def analyst_actions(position, key, today, days=5):
    """Analysten-Aktionen (Hoch-/Herabstufung, Kursziel) zu einer Aktie aus den letzten `days` Tagen."""
    frm = (today - timedelta(days=days)).isoformat()
    rows = _get("company-news", key, symbol=position["proxy"], **{"from": frm, "to": today.isoformat()})
    name = re.compile(r"\b" + re.escape(position["keywords"][0]), re.I)
    out, seen = [], set()
    for n in rows:
        head = n.get("headline", "")
        if (n.get("source", "").lower() not in ANALYST_SOURCES or not ACTION.search(head)
                or not (name.search(head) or position["proxy"] in head) or "Analyst Blog" in head):
            continue
        if head in seen:
            continue
        seen.add(head)
        d = "up" if UP.search(head) and not DOWN.search(head) else "down" if DOWN.search(head) and not UP.search(head) else "mixed"
        out.append({"headline": head, "source": n.get("source"), "url": n.get("url"),
                    "date": n.get("datetime"), "dir": d})
    return sorted(out, key=lambda x: -(x["date"] or 0))[:5]


def has(text, words):
    return [w for w in words if re.search(r"\b" + re.escape(w) + r"\b", text, re.I)]


def _ok(n):
    return n.get("source", "").lower() in TRUSTED and not CLICKBAIT.search(n.get("headline", ""))


def general(key):
    return [n for n in _get("news", key, category="general") if _ok(n)]


def company(symbol, key, today):
    frm = (today - timedelta(days=2)).isoformat()
    return [n for n in _get("company-news", key, symbol=symbol, **{"from": frm, "to": today.isoformat()}) if _ok(n)]


def classify(n, pf, cfg, held_symbol=None):
    """Bewertet eine Nachricht: Welche Positionen? Big Player? Dringend?"""
    text = f"{n.get('headline', '')} {n.get('summary', '')}"
    head = n.get("headline", "")
    affected = {held_symbol} if held_symbol else set()
    stocks = {held_symbol} if held_symbol else set()
    for p in pf["positions"]:
        if has(text, p.get("keywords", [])):
            affected.add(p["proxy"])
            if p["type"] == "stock":
                stocks.add(p["proxy"])
    macro = has(text, cfg["macro_keywords"])
    players = has(head, cfg["big_players"])
    market = has(text, cfg["market_keywords"])
    strong = has(head, cfg["strong_words"])
    relevant = bool(affected or macro or (players and market))
    # Sofort nur: Big Player mit Marktbezug, oder starke Nachricht zu einer eigenen Einzelaktie.
    # Makro-News und Themen, die nur ETFs berühren, kommen ins stündliche Update.
    urgent = bool((players and (market or affected)) or (stocks and strong))
    return {
        "relevant": relevant, "urgent": urgent,
        "affected": sorted(a for a in affected if a),
        "players": players, "macro": macro, "strong": strong,
    }
