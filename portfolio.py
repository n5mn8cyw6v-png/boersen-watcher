"""Demo-Portfolio: Wert in Euro aus US-Vergleichskursen (Proxy) und EZB-Kurs.

Wert jetzt = Startwert € × (Proxy jetzt / Proxy Start) × (EUR/USD Start / EUR/USD jetzt)
"""

import re

import requests

ECB_DAILY = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
SPECULATIVE = {"stock"}  # Einzelaktien gelten als riskanter als breite ETFs
TECH_PROXIES = {"XLK", "NVDA", "IONQ", "BLOK"}


def eurusd():
    r = requests.get(ECB_DAILY, timeout=20)
    r.raise_for_status()
    return float(re.search(r"currency='USD' rate='([\d.]+)'", r.text).group(1))


def evaluate(pf, quotes, fx_now, base):
    """base: {"fx": float, "proxy": {sym: price}}. Wird beim ersten Lauf gesetzt."""
    rows, total, cost, day_abs = [], 0.0, 0.0, 0.0
    for p in pf["positions"]:
        sym = p["proxy"]
        q = quotes.get(sym)
        start_px = base["proxy"].get(sym)
        if q and start_px:
            value = p["value_eur"] * (q["c"] / start_px) * (base["fx"] / fx_now)
            day_pct = q.get("dp") or 0.0
        else:
            value, day_pct = p["value_eur"], None
        day_abs += value - value / (1 + (day_pct or 0) / 100)
        total += value
        cost += p["cost_eur"]
        rows.append({
            "name": p["name"], "short": p["short"], "proxy": sym, "type": p["type"],
            "value": round(value, 2), "cost": p["cost_eur"],
            "pl_pct": round((value / p["cost_eur"] - 1) * 100, 2),
            "day_pct": None if day_pct is None else round(day_pct, 2),
        })
    for r in rows:
        r["weight"] = round(r["value"] / total * 100, 1) if total else 0
    rows.sort(key=lambda r: -r["value"])
    day_pct = day_abs / (total - day_abs) * 100 if total - day_abs else 0
    return {
        "goal": pf.get("goal", ""),
        "total": round(total, 2), "cost": round(cost, 2),
        "pl": round(total - cost, 2), "pl_pct": round((total / cost - 1) * 100, 2) if cost else 0,
        "day_pct": round(day_pct, 2), "day_abs": round(day_abs, 2),
        "positions": rows,
    }


def risk_check(ev):
    """Sachliche Hinweise im Vergleich zum Ziel 'langfristig, mittleres Risiko'. Keine Empfehlung."""
    notes = []
    pos = ev["positions"]
    if pos and pos[0]["weight"] > 40:
        notes.append(f"Klumpenrisiko: {pos[0]['short']} macht {pos[0]['weight']:.0f} % des Depots aus. "
                     "Bei mittlerem Risiko gilt meist: keine einzelne Position über ca. 30–40 %.")
    tech = sum(r["weight"] for r in pos if r["proxy"] in TECH_PROXIES)
    if tech > 50:
        notes.append(f"Tech-Anteil ca. {tech:.0f} %: Das Depot hängt stark an Technologie. "
                     "Fällt Tech, fällt das Depot deutlich stärker als der Gesamtmarkt.")
    single = sum(r["weight"] for r in pos if r["type"] in SPECULATIVE)
    if single > 20:
        notes.append(f"Einzelaktien ca. {single:.0f} %: Einzelwerte schwanken stärker als breite ETFs.")
    broad = sum(r["weight"] for r in pos if r["proxy"] in {"URTH", "VT", "IEMG"})
    notes.append(f"Breit gestreute ETFs (Welt, Schwellenländer): ca. {broad:.0f} % – das ist der ruhige Kern für langfristiges Wachstum.")
    return notes
