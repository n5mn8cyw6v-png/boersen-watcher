"""Einfache Erklärungen: Was ist das? Wie reagiert der Markt typischerweise?

Das sind Faustregeln aus der Vergangenheit, keine Vorhersage.
"""

SEC = {
    "2.02": ("Die Firma veröffentlicht Umsatz und Gewinn der letzten 3 Monate (Quartalszahlen).",
             "Entscheidend ist der Vergleich mit den Erwartungen der Analysten und der Ausblick. "
             "Besser als erwartet → Kurs steigt oft, schlechter → fällt oft. Bewegungen von ±5–10 % sind normal."),
    "2.01": ("Eine Übernahme oder ein Verkauf eines Geschäftsteils ist abgeschlossen.",
             "Meist schon vorher bekannt, daher oft wenig Bewegung."),
    "1.01": ("Die Firma hat einen wichtigen Vertrag unterschrieben (z. B. Großkunde, Kredit, Partnerschaft).",
             "Wirkung hängt vom Inhalt ab: großer Kunde eher positiv, neue Schulden eher neutral."),
    "5.02": ("Wechsel im Vorstand oder Aufsichtsrat (jemand kommt oder geht).",
             "Plötzlicher Abgang des Chefs → Kurs fällt oft kurz. Geplante Wechsel → meist wenig Wirkung."),
    "4": ("Ein Manager oder Großaktionär hat Aktien der eigenen Firma gekauft oder verkauft.",
          "Verkäufe sind oft lange geplant (Gehalt in Aktien) und wenig aussagekräftig. "
          "Käufe aus eigener Tasche gelten eher als positives Zeichen."),
    "10-Q": ("Ausführlicher Quartalsbericht mit allen Details.",
             "Die wichtigen Zahlen sind meist schon bekannt → selten große Bewegung."),
    "10-K": ("Ausführlicher Jahresbericht mit allen Details und Risiken.",
             "Zahlen meist schon bekannt → selten große Bewegung."),
    "8-K": ("Eine wichtige Mitteilung der Firma an die Börsenaufsicht.",
            "Wirkung hängt vom Inhalt ab, Link öffnen."),
}

IPO = ("Eine Firma bringt Aktien an die Börse (IPO) oder gibt neue Aktien aus. Der Preis steht jetzt fest.",
       "Für dich meist nur interessant bei bekannten Namen. Neue Aktien schwanken in den ersten Wochen oft stark.")

FED = [
    ("FOMC statement", "Die US-Notenbank hat über den Leitzins entschieden.",
     "Zinssenkung oder lockere Signale → Aktien steigen oft (besonders Tech). "
     "Höhere Zinsen oder strenge Signale → Aktien fallen oft. Wichtig ist, was erwartet wurde."),
    ("discount rate", "Protokoll zum Diskontsatz (Zins für Notkredite an Banken).",
     "Für Aktien meist kaum relevant."),
    ("schedule", "Die Fed hat ihren Terminkalender für Sitzungen veröffentlicht oder geändert.",
     "Keine direkte Wirkung auf Kurse."),
    ("Minutes", "Protokoll einer früheren Fed-Sitzung: zeigt, wie die Mitglieder denken.",
     "Meist kleinere Bewegung, außer es gibt Überraschungen zur Zinsrichtung."),
    ("", "Mitteilung der US-Notenbank (Fed).",
     "Wirkung hängt vom Inhalt ab. Alles, was auf sinkende Zinsen deutet, gilt eher als gut für Aktien."),
]

AGENDA = {
    "Consumer Price Index": ("Inflation (CPI)", "Wie stark die Preise für Verbraucher steigen.",
                             "Höher als erwartet → Zinssenkungen unwahrscheinlicher → Aktien fallen oft. Niedriger → steigen oft."),
    "Personal Income and Outlays": ("Konsum & PCE-Inflation", "Lieblings-Inflationszahl der Fed, dazu Einkommen und Konsum.",
                                    "Wie beim CPI: höhere Inflation als erwartet ist eher schlecht für Aktien."),
    "Producer Price Index": ("Erzeugerpreise (PPI)", "Preise, die Firmen für Vorprodukte zahlen: Vorbote für die Inflation.",
                             "Höher als erwartet → eher schlecht für Aktien."),
    "Employment Situation": ("Arbeitsmarktbericht", "Neue Jobs und Arbeitslosenquote in den USA.",
                             "Zwiespältig: Viele Jobs = starke Wirtschaft, kann aber Zinssenkungen verzögern. Sehr schwache Zahlen → Angst vor Rezession."),
    "Job Openings": ("Offene Stellen (JOLTS)", "Wie viele Stellen Firmen ausgeschrieben haben.",
                     "Meist kleinere Wirkung, zeigt wie heiß der Arbeitsmarkt ist."),
    "Gross Domestic Product": ("BIP", "Wie stark die US-Wirtschaft gewachsen ist.",
                               "Starkes Wachstum ist meist gut, solange die Inflation nicht steigt."),
    "Retail Sales": ("Einzelhandelsumsätze", "Wie viel die Amerikaner einkaufen.",
                     "Starker Konsum = starke Wirtschaft. Wirkung meist mittel."),
    "Real Earnings": ("Reallöhne", "Löhne nach Abzug der Inflation.",
                      "Kommt zusammen mit dem CPI, meist ohne eigene Wirkung."),
    "FOMC": ("Fed-Zinsentscheid", "Die US-Notenbank entscheidet über den Leitzins (Pressekonferenz 30 Min. später).",
             "Einer der wichtigsten Termine: Zinssenkung oder lockere Worte → Aktien steigen oft, sonst fallen oft."),
}


def sec(form, item_nums):
    for n in ("2.02", "2.01", "1.01", "5.02"):
        if n in item_nums:
            return SEC[n]
    return SEC.get(form, SEC["8-K"])


def fed(title):
    for key, what, typical in FED:
        if key.lower() in title.lower():
            return what, typical
    return FED[-1][1:]


def agenda(title):
    for key, val in AGENDA.items():
        if key.lower() in title.lower():
            return val
    return (title, "", "")
