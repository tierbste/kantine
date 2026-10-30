#!/usr/bin/env python3
"""Erzeugt dashboard/kantine.json - das Grafana-Dashboard zum Exporter.

    python3 dashboard/build.py

Nur Standardbibliothek.  Die JSON-Datei ist das Build-Artefakt und wird mit
eingecheckt, damit man sie direkt in Grafana importieren kann.

Aufbau: eine Kachel je Ausgabe (Reihenfolge = AUSGABEN), nur fuer den
aktuellen Tag.  Die Kachel selbst ist fertiges HTML aus dem Exporter-Label
'kachel'; die versteckte Variable 'ausgabe' holt es per query_result() und
ein Textpanel wiederholt sich je Wert.  Warum so umstaendlich: Grafanas
Canvas kennt kein font-weight, und Unicode-Fettschrift verschiebt im Browser
die Umlaut-Punkte - nur das Textpanel rendert echtes HTML.
"""

import json
import os

# Spalten in dieser Reihenfolge; was es an dem Tag nicht gibt, bekommt keine
# Kachel.  Ausgaben, die hier fehlen (z. B. "Mittag N1"), werden nie gezeigt.
AUSGABEN = ["Spezial", "Menü 1", "Menü 2", "Street Food", "Suppe"]
# Kein '$' (label_replace) und kein '"' (Regex der Variable) im HTML.
LEER_HTML = ("<div style='font-size:22px;font-weight:700'>"
             "Kein Speiseplan veröffentlicht</div>")


def dashboard(auswahl='aktuell="1"', uid="kantine", title="Kantine"):
    """auswahl: Label-Filter; zum Testen z. B. 'datum="2026-10-29"'."""
    ds = {"type": "prometheus", "uid": "${datasource}"}
    # topk/timestamp: nach einem Pod-Neustart haelt Prometheus die Serien des
    # alten Pods noch ~5 min - je Gericht zaehlt nur die juengste.
    # '* 0 + i' macht den Messwert zur Position, sort() ordnet danach.
    rang = " or ".join(
        f'(topk by (gericht) (1, timestamp(kantine_gericht_info{{{auswahl},kategorie="{k}"}}))'
        f' * 0 + {i})'
        for i, k in enumerate(AUSGABEN, start=1))
    # Gibt es gar nichts, liefert absent() eine Ersatz-Kachel.
    leer = (f'label_replace(label_replace(absent(kantine_gericht_info{{{auswahl}}}) * 99, '
            f'"kachel", "{LEER_HTML}", "", ""), "kategorie", "keiner", "", "")')
    var_query = f"query_result(sort({rang} or {leer}))"

    panel = {
        "id": 1,
        "type": "text",
        "title": "",
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 6},
        "repeat": "ausgabe",
        "repeatDirection": "h",
        "maxPerRow": len(AUSGABEN),
        # :raw - sonst escaped das Textpanel das HTML aus der Variable.
        "options": {"mode": "html", "content": "${ausgabe:raw}",
                    "code": {"language": "plaintext", "showLineNumbers": False,
                             "showMiniMap": False}},
    }
    return {
        "uid": uid,
        "title": title,
        "tags": ["kantine"],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 0,
        "editable": True,
        "graphTooltip": 0,
        "refresh": "5m",
        "time": {"from": "now-1h", "to": "now"},
        "timepicker": {"hidden": True},
        "annotations": {"list": []},
        "templating": {"list": [
            # Waehlt beim Import automatisch die Standard-Prometheus-Quelle;
            # gibt es mehrere (OpenShift: Plattform + User Workload), hier
            # die mit den Kantinen-Metriken auswaehlen und speichern.
            {"type": "datasource", "name": "datasource", "label": "Datenquelle",
             "query": "prometheus", "regex": "", "hide": 0, "refresh": 1,
             "includeAll": False, "multi": False, "current": {}, "options": []},
            # Traegt die Kacheln: Wert = HTML, Text = Ausgabe.  query_result()
            # reiht die Labels alphabetisch mit ',' auf - 'kachel' steht direkt
            # vor 'kategorie'.
            {"type": "query", "name": "ausgabe", "label": "Ausgabe", "hide": 2,
             "datasource": ds,
             "query": {"qryType": 3, "query": var_query,
                       "refId": "PrometheusVariableQueryEditor-VariableQuery"},
             "definition": var_query,
             "regex": '/kachel="(?<value>[^"]*)",kategorie="(?<text>[^"]*)"/',
             "sort": 0,      # Reihenfolge der Abfrage behalten (sort() oben)
             "refresh": 2,   # bei jedem Refresh neu, sonst bleibt der alte Tag
             "multi": True, "includeAll": True,
             "current": {"text": ["All"], "value": ["$__all"]}, "options": []},
        ]},
        "panels": [panel],
    }


def main():
    pfad = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kantine.json")
    with open(pfad, "w") as f:
        json.dump(dashboard(), f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(pfad)


if __name__ == "__main__":
    main()
