"""Englische Nachrichten ins Deutsche übersetzen (kostenloser Dienst MyMemory) und Fachbegriffe erklären.

MyMemory erlaubt ohne Anmeldung ca. 5000 Zeichen pro Tag. Deshalb: Tageszähler im State,
Cache für bereits übersetzte Texte, und bei leerem Kontingent bleibt der englische Text stehen.
"""

import html
import re

import requests

API = "https://api.mymemory.translated.net/get"
DAILY_LIMIT = 4500

# Englische Fachwörter vor der Übersetzung in einfache Wörter umschreiben,
# damit die Maschine sie nicht wörtlich (und unverständlich) übersetzt.
PLAIN_EN = [
    (r"\bpremarket\b|\bpre-market\b", "before the stock exchange opens"),
    (r"\bafter-hours\b|\bafter hours\b", "after the stock exchange closed"),
    (r"\bsell-off\b|\bselloff\b", "sharp fall"),
    (r"\brall(y|ies|ied)\b", "rise"),
    (r"\bslump(s|ed)?\b|\btumble(s|d)?\b|\bplunge(s|d)?\b|\bsink(s)?\b|\bsank\b", "falls sharply"),
    (r"\bsoar(s|ed)?\b|\bsurge(s|d)?\b|\bjump(s|ed)?\b|\bskyrocket(s|ed)?\b", "rises sharply"),
    (r"\bbeat(s)? estimates\b", "is better than expected"),
    (r"\bmiss(es|ed)? estimates\b", "is worse than expected"),
    (r"\bQ([1-4])\b", r"quarter \1"),
]

# Sperrige deutsche Maschinen-Übersetzungen vereinfachen
PLAIN_DE = [
    (r"des beizulegenden Zeitwerts|beizulegende[nr]? Zeitwert(s|es)?", "des fairen Werts"),
    (r"Leerverkäufe", "Wetten auf fallende Kurse"),
    (r"Marktkapitalisierung", "Börsenwert"),
    (r"Gewinnprognose", "Gewinn-Erwartung"),
    (r"„Buy“|\bBuy\b", "„Kaufen“"), (r"„Sell“|\bSell\b", "„Verkaufen“"), (r"„Hold“|\bHold\b", "„Halten“"),
]

# Begriffe, die in der Meldung vorkommen können, mit einfacher Erklärung
TERMS = [
    (r"price target|fair value", "Kursziel", "Preis, den ein Analyst für die Aktie in ca. 12 Monaten erwartet."),
    (r"upgrade", "Hochstufung", "Ein Analyst hat seine Meinung zur Aktie verbessert (z. B. von „Halten“ auf „Kaufen“)."),
    (r"downgrade", "Herabstufung", "Ein Analyst hat seine Meinung zur Aktie verschlechtert."),
    (r"outperform|overweight", "Outperform/Overweight", "Analyst erwartet, dass die Aktie besser läuft als der Markt."),
    (r"underperform|underweight", "Underperform/Underweight", "Analyst erwartet, dass die Aktie schlechter läuft als der Markt."),
    (r"earnings|quarterly results|revenue", "Quartalszahlen", "Umsatz und Gewinn der letzten 3 Monate. Vergleicht man mit den Erwartungen."),
    (r"guidance|outlook|forecast", "Ausblick", "Was die Firma selbst für die nächsten Monate erwartet. Oft wichtiger als die Zahlen."),
    (r"premarket|pre-market|after-hours", "Vor-/Nachbörslich", "Handel vor 15:30 bzw. nach 22:00 Uhr (deutsche Zeit). Kurse dort sind weniger verlässlich."),
    (r"\bIPO\b|initial public offering", "Börsengang", "Eine Firma bietet zum ersten Mal Aktien an der Börse an."),
    (r"buyback|repurchase", "Aktienrückkauf", "Die Firma kauft eigene Aktien zurück. Gilt meist als gutes Zeichen."),
    (r"dividend", "Dividende", "Gewinnanteil, den die Firma an Aktionäre auszahlt."),
    (r"short seller|short interest|short selling", "Leerverkäufer", "Anleger, die auf fallende Kurse wetten."),
    (r"market cap|market value", "Börsenwert", "Anzahl Aktien × Kurs = was die ganze Firma an der Börse wert ist."),
    (r"\byields?\b|treasur", "Anleihezinsen", "Zinsen auf US-Staatsanleihen. Steigen sie, fallen Aktien oft."),
    (r"rate cut|rate hike|interest rate|\bFed\b|FOMC", "Leitzins", "Zins der US-Notenbank. Sinkt er, hilft das meist Aktien."),
    (r"inflation|\bCPI\b|\bPCE\b", "Inflation", "Wie schnell die Preise steigen. Hohe Inflation = höhere Zinsen wahrscheinlicher."),
    (r"tariff", "Zölle", "Abgaben auf importierte Waren. Belasten betroffene Firmen."),
    (r"bull|bullish", "Bullish", "Erwartung steigender Kurse."),
    (r"bear|bearish", "Bearish", "Erwartung fallender Kurse."),
    (r"\bETF\b", "ETF", "Fonds, der viele Aktien auf einmal enthält, z. B. einen ganzen Index."),
    (r"\bstake\b", "Beteiligung", "Anteil an einer Firma."),
    (r"acquisition|acquire|merger|takeover", "Übernahme", "Eine Firma kauft eine andere ganz oder teilweise."),
    (r"rare earth", "Seltene Erden", "Rohstoffe für Magnete, Elektroautos und Chips. China dominiert den Markt."),
]


def terms(text, limit=3):
    out = []
    for pat, word, expl in TERMS:
        if re.search(pat, text, re.I) and len(out) < limit:
            out.append(f"{word}: {expl}")
    return out


def _plain(text):
    for pat, rep in PLAIN_EN:
        text = re.sub(pat, rep, text, flags=re.I)
    return text


class Translator:
    def __init__(self, state, today):
        b = state.setdefault("tr_budget", {})
        if b.get("day") != today:
            b.update(day=today, chars=0)
        self.budget = b
        self.cache = state.setdefault("tr_cache", {})
        self.off = False

    def __call__(self, text):
        text = (text or "").strip()
        if not text:
            return text
        if text in self.cache:
            return self.cache[text]
        if self.off or self.budget["chars"] + len(text) > DAILY_LIMIT:
            return None
        try:
            r = requests.get(API, params={"q": _plain(text)[:480], "langpair": "en|de"}, timeout=20)
            r.raise_for_status()
            d = r.json()
            out = html.unescape(d["responseData"]["translatedText"] or "")
            for pat, rep in PLAIN_DE:
                out = re.sub(pat, rep, out)
            if d.get("responseStatus") != 200 or not out or "MYMEMORY WARNING" in out.upper():
                self.off = True  # Kontingent leer oder Dienst gestört: Rest bleibt englisch
                return None
        except Exception:
            self.off = True
            return None
        self.budget["chars"] += len(text)
        self.cache[text] = out
        if len(self.cache) > 300:
            for k in list(self.cache)[:-300]:
                del self.cache[k]
        return out
