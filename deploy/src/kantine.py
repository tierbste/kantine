#!/usr/bin/env python3
"""Kantinen-Speiseplan (App "Mein Aramark Restaurant", Backend qnips) als
Prometheus-Metriken unter :9100/metrics.  Nur Standardbibliothek.

Ablauf, nachgebaut aus der App de.aramark.new 2026.1.14:
  1. GET  cons/api/NewIdentity    anonyme Identitaet -> ApiKey
  2. POST cons/api/Stores/Unlock  Standort-PIN -> Store-IDs
  3. GET  cons/api/FirebaseInfo   Custom Token -> Firebase-Login
  4. Firestore .../Stores/{id}                     -> Menue-IDs
     Firestore .../Menus/{id}/Years/{J}/Weeks/{KW} -> Wochenplan

Der letzte Stand liegt nur im Speicher; nach einem Neustart wird neu geholt.
"""

import datetime as dt
import html
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
import zoneinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIN = os.environ["KANTINE_PIN"]
REFRESH = int(os.environ.get("REFRESH_SECONDS", "21600"))
RETRY = int(os.environ.get("RETRY_SECONDS", "900"))
TZ = zoneinfo.ZoneInfo("Europe/Berlin")

API = "https://api.qnips.com/cons/api/"
# Firebase-Projekt der App (res/values/strings.xml) - steckt in jeder APK.
SIGN_IN = ("https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
           "?key=AIzaSyAVHnoTvWzc-AbJv4Hwc5Wmks9j4bzNV-8")
FIRESTORE = "https://firestore.googleapis.com/v1/projects/q-aramark/databases/(default)/documents"
SPRACHE = "de-DE"  # Firestore-Pfad haengt an der Profilsprache
# Wie die App sie schickt (io.qnips.api.Backend).
HEADERS = {
    "Accept": "application/json",
    "App-Brand": "Aramark",
    "App-Feature-Set": "24368",
    "Response-Version": "v3",
    "User-Agent": "Aramark/2026.1.14 (Android 14 Build/UQ1A.240205.004; Google Pixel 7)",
}
GRAU = "#9aa3ad"

log = logging.getLogger("kantine")
ident = None            # anonyme Identitaet, gilt bis zum naechsten Fehler
stand = ([], 0.0)       # (Gerichte, Zeitpunkt des letzten Erfolgs) - nur als Ganzes ersetzt


def http(method, url, headers, body=None):
    """JSON rein, JSON raus.  404 -> None, andere Fehler -> Exception."""
    req = urllib.request.Request(url, method=method, headers=dict(headers))
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or "null")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        # Ohne Query-String - dort steht beim Login der API-Key.
        raise RuntimeError(f"{method} {url.split('?')[0]}: HTTP {e.code}") from None


def fs(wert):
    """Firestore-REST-Wert ({"stringValue": ...}) -> Python."""
    (art, v), = wert.items()
    if art == "mapValue":
        return {k: fs(x) for k, x in v.get("fields", {}).items()}
    if art == "arrayValue":
        return [fs(x) for x in v.get("values", [])]
    return int(v) if art == "integerValue" else v


def abrufen():
    """Alle Gerichte der aktuellen und der naechsten Woche."""
    global ident
    if ident is None:
        ident = http("GET", API + "NewIdentity", HEADERS)
    qnips = dict(HEADERS, Authorization="0" + ident["ApiKey"])
    stores = http("POST", API + "Stores/Unlock", qnips, {"Type": 0, "Code": PIN})  # 1 = QR-Code
    fb = http("GET", API + "FirebaseInfo", qnips)
    login = http("POST", SIGN_IN, {"X-Android-Package": "de.aramark.new"},
                 {"token": fb["CustomToken"], "returnSecureToken": True})
    firebase = {"Authorization": "Bearer " + login["idToken"]}
    basis = f"{FIRESTORE}/{fb['Environment']}/{SPRACHE}"

    heute = dt.datetime.now(TZ).date()
    gerichte = []
    for sid in stores:
        store = fs({"mapValue": http("GET", f"{basis}/Stores/{sid}", firebase)})
        for menue in store.get("Menus") or []:
            if not menue.get("ShowInApp"):
                continue
            for tag in (heute, heute + dt.timedelta(days=7)):
                jahr, kw, _ = tag.isocalendar()
                woche = http("GET", f"{basis}/Menus/{menue['Id']}/Years/{jahr}/Weeks/{kw}", firebase)
                if woche:  # None = Woche noch nicht veroeffentlicht
                    gerichte += aufbereiten(fs({"mapValue": woche}))
    return gerichte


def aufbereiten(woche):
    """Wochen-Dokument -> ein dict je Gericht, Texte schon fertig formatiert."""
    labels = {(a.get("AllergenId"), a.get("SubAllergenId")): a.get("Label")
              for a in woche.get("Allergens") or []}
    gerichte = []
    for tag in woche.get("Days") or []:
        for kat in tag.get("Categories") or []:
            for p in kat.get("Products") or []:
                # Preis ohne Tag = Mitarbeiter, "Gästepreis" interessiert nicht.
                cent = next((x.get("LocalizablePrice", {}).get("Amount")
                             for x in p.get("Prices") or [] if not x.get("Tag")), None)
                # [0, 6] + SubAllergens0 [0] -> "A1, G"
                allergene = [labels.get((a, s)) for a in p.get("Allergens") or []
                             for s in p.get(f"SubAllergens{a}") or [None]]
                teile = (t.strip() for t in re.split(r"[|\n]", p.get("Name") or ""))
                gerichte.append({
                    "datum": tag["Date"][:10],
                    "kategorie": kat.get("Name") or "",
                    "gericht": " | ".join(t for t in teile if t),
                    "preis": "" if cent is None else f"{cent / 100:.2f} €".replace(".", ","),
                    "kcal": (p.get("NutritionFacts") or {}).get("KCalPerServing"),
                    "allergene": ", ".join(a for a in allergene if a),
                    "kennzeichnung": ", ".join(t["Name"] for t in p.get("CustomTags") or []),
                })
    return gerichte


def kachel(g):
    """Fertige Dashboard-Kachel.  Grafana zerlegt sie per Regex - deshalb nie
    ein doppeltes Anfuehrungszeichen: Attribute in '...', Texte escaped."""
    e = html.escape
    hinweise = " · ".join(t for t in (g["allergene"] and "Allergene: " + g["allergene"],
                                      g["kennzeichnung"]) if t)
    kcal = "" if g["kcal"] is None else f"{g['kcal']} kcal"
    return ("<div style='display:flex;flex-direction:column;height:100%'>"
            f"<div style='font-size:22px;font-weight:700;line-height:1.2'>{e(g['kategorie'])}</div>"
            f"<div style='font-size:16px;margin-top:8px'>{e(g['gericht'])}</div>"
            f"<div style='margin-top:auto;font-size:12px;color:{GRAU}'>{e(hinweise)}</div>"
            "<div style='display:flex;justify-content:space-between;margin-top:6px;"
            f"font-size:13px;color:{GRAU}'><span>{e(g['preis'])}</span><span>{e(kcal)}</span></div>"
            "</div>")


def esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def metriken(gerichte, erfolg, heute=None):
    heute = heute or dt.datetime.now(TZ).date().isoformat()
    # "aktuell" = heute; am Wochenende der naechste Tag mit Plan.
    aktuell = min((g["datum"] for g in gerichte if g["datum"] >= heute), default=None)
    zeilen = []
    for g in gerichte:
        labels = dict(g, kcal="" if g["kcal"] is None else g["kcal"],
                      aktuell=int(g["datum"] == aktuell), kachel=kachel(g))
        text = ",".join(f'{k}="{esc(v)}"' for k, v in labels.items())
        zeilen.append(f"kantine_gericht_info{{{text}}} 1")
    zeilen.append(f"kantine_abruf_letzter_erfolg_timestamp_seconds {erfolg:.0f}")
    return "\n".join(zeilen) + "\n"


def schleife():
    global ident, stand
    while True:
        try:
            gerichte = abrufen()
            stand = (gerichte, time.time())
            log.info("%d Gerichte an %d Tagen", len(gerichte), len({g["datum"] for g in gerichte}))
            time.sleep(REFRESH)
        except Exception:
            log.exception("Abruf fehlgeschlagen")
            ident = None  # naechster Versuch mit frischer Identitaet
            time.sleep(RETRY)


class Metriken(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.end_headers()
        self.wfile.write(metriken(*stand).encode())

    def log_message(self, *args):  # Scrapes nicht ins Log
        pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=schleife, daemon=True).start()
    ThreadingHTTPServer(("", 9100), Metriken).serve_forever()
