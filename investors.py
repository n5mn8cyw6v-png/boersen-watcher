"""Was machen Großinvestoren? Auswertung der 13F-Depotmeldungen bei der SEC.

Vergleicht das letzte gemeldete Quartal mit dem davor: neu gekauft, aufgestockt,
reduziert, komplett verkauft. Das sind Entscheidungen der Investoren, keine Empfehlung.
Achtung: 13F-Daten sind bis zu 45 Tage nach Quartalsende alt.
"""

import re
import time
import xml.etree.ElementTree as ET

import requests

SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik:0>10}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
NS = "{http://www.sec.gov/edgar/document/thirteenf/informationtable}"


def _get(url, contact):
    r = requests.get(url, headers={"User-Agent": f"BoersenWatcher {contact}"}, timeout=30)
    r.raise_for_status()
    time.sleep(0.5)  # SEC: höchstens wenige Anfragen pro Sekunde
    return r


def latest_filings(cik, contact, n=2):
    rec = _get(SUBMISSIONS.format(cik=cik), contact).json()["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if form == "13F-HR":
            out.append({"acc": rec["accessionNumber"][i], "filed": rec["filingDate"][i], "period": rec["reportDate"][i]})
            if len(out) == n:
                break
    return out


def holdings(cik, acc, contact):
    base = ARCHIVE.format(cik=int(cik), acc=acc.replace("-", ""))
    items = _get(base + "index.json", contact).json()["directory"]["item"]
    xml = next(i["name"] for i in items if i["name"].endswith(".xml") and i["name"] != "primary_doc.xml")
    root = ET.fromstring(_get(base + xml, contact).content)
    agg = {}
    for t in root.iter(f"{NS}infoTable"):
        if t.findtext(f"{NS}putCall"):
            continue  # Optionen auslassen, nur echte Aktien
        raw = t.findtext(f"{NS}nameOfIssuer").strip()
        # Die ersten 6 Zeichen der CUSIP bezeichnen den Emittenten: fasst Aktienklassen zusammen
        # und ist (anders als der Name) in jedem Quartal gleich geschrieben.
        issuer = (t.findtext(f"{NS}cusip") or raw)[:6]
        h = agg.setdefault(issuer, {"name": raw, "shares": 0, "value": 0})
        if len(raw) > len(h["name"]):
            h["name"] = raw
        h["shares"] += int(float(t.findtext(f"{NS}shrsOrPrnAmt/{NS}sshPrnamt") or 0))
        h["value"] += int(float(t.findtext(f"{NS}value") or 0))
    return agg


def nice(name):
    """'NVIDIA CORPORATION' -> 'Nvidia Corporation' (lesbarer)."""
    return re.sub(r"\b(Inc|Corp|Co|Ltd|Plc|Hldgs|Hldg|Com|Cl A|Cl B|New|Del)\b\.?", "", name.title()).strip(" ,.")


def compare(prev, cur, top=5):
    total = sum(h["value"] for h in cur.values()) or 1
    def entry(c, h, change=None):
        return {"name": nice(h["name"]), "raw": h["name"], "value_musd": round(h["value"] / 1e6, 1),
                "weight": round(h["value"] / total * 100, 1), "change_pct": change}
    new, up, down = [], [], []
    for c, h in cur.items():
        p = prev.get(c)
        if not p or not p["shares"]:
            new.append(entry(c, h))
        elif h["shares"] >= p["shares"] * 1.10:
            up.append(entry(c, h, round((h["shares"] / p["shares"] - 1) * 100)))
        elif h["shares"] <= p["shares"] * 0.90:
            down.append(entry(c, h, round((h["shares"] / p["shares"] - 1) * 100)))
    exits = [{"name": nice(p["name"]), "raw": p["name"], "value_musd": round(p["value"] / 1e6, 1)}
             for c, p in prev.items() if c not in cur]
    by_val = lambda x: -x["value_musd"]
    largest = sorted((entry(c, h) for c, h in cur.items()), key=by_val)[:top]
    return {"new": sorted(new, key=by_val)[:top], "up": sorted(up, key=by_val)[:top],
            "down": sorted(down, key=by_val)[:top], "exits": sorted(exits, key=by_val)[:top],
            "largest": largest, "count": len(cur), "total_musd": round(total / 1e6)}


def analyze(name, cik, contact):
    filings = latest_filings(cik, contact)
    if not filings:
        return None
    cur = holdings(cik, filings[0]["acc"], contact)
    prev = holdings(cik, filings[1]["acc"], contact) if len(filings) > 1 else {}
    return {"investor": name, "acc": filings[0]["acc"], "filed": filings[0]["filed"],
            "period": filings[0]["period"], **compare(prev, cur)}


def overlap(summary, pf):
    """Welche Depot-Positionen tauchen bei diesem Investor auf (in irgendeiner Liste)?"""
    hits = []
    for p in pf["positions"]:
        if p["type"] != "stock":
            continue
        pat = re.compile(re.escape(p["keywords"][0]), re.I)
        for kind in ("new", "up", "down", "exits", "largest"):
            for h in summary.get(kind, []):
                if pat.search(h["raw"]):
                    hits.append({"position": p["short"], "kind": kind, "change_pct": h.get("change_pct")})
    return hits


def short_text(s):
    """Kurzfassung für die Push-Nachricht."""
    part = lambda label, xs: f"{label}: " + ", ".join(x["name"] for x in xs[:3]) if xs else None
    bits = [part("Neu gekauft", s["new"]), part("Aufgestockt", s["up"]),
            part("Reduziert", s["down"]), part("Komplett verkauft", s["exits"])]
    return "\n".join(b for b in bits if b) or "Kaum Veränderungen."


KIND_DE = {"new": "neu gekauft", "up": "aufgestockt", "down": "reduziert", "exits": "komplett verkauft",
           "largest": "unter den größten Positionen"}
