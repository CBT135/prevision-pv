"""Prix day-ahead France au pas 15 min (plateforme de transparence ENTSO-E).

Ce sont les résultats de l'enchère SDAC (couplage européen, opérée notamment par EPEX SPOT),
publiés vers 13 h la veille. Secret attendu : ENTSOE_TOKEN (jeton gratuit de la plateforme).
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import pandas as pd
import requests

from .commun import TZ, journal, secret

URL = "https://web-api.tp.entsoe.eu/api"
ZONE_FR = "10YFR-RTE------C"


def _sans_ns(racine: ET.Element) -> ET.Element:
    for el in racine.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return racine


def analyser(xml: str) -> pd.Series:
    """XML ENTSO-E A44 -> série €/MWh indexée par fin de quart d'heure UTC."""
    r = _sans_ns(ET.fromstring(xml))
    series = {"PT15M": [], "PT30M": [], "PT60M": []}
    for ts in r.iter("TimeSeries"):
        for per in ts.iter("Period"):
            debut = pd.Timestamp(per.find("timeInterval/start").text)
            fin = pd.Timestamp(per.find("timeInterval/end").text)
            reso = per.find("resolution").text
            pas = pd.Timedelta(reso.replace("PT", "").lower().replace("m", "min"))
            n = int((fin - debut) / pas)
            pts = {int(p.find("position").text): float(p.find("price.amount").text) for p in per.iter("Point")}
            vals, dernier = [], None
            for pos in range(1, n + 1):  # courbe A03 : un point absent reprend la valeur précédente
                dernier = pts.get(pos, dernier)
                vals.append(dernier)
            idx = pd.date_range(debut + pas, fin, freq=pas)
            series.setdefault(reso, []).append(pd.Series(vals, index=idx, dtype=float))
    for reso in ("PT15M", "PT30M", "PT60M"):
        if series.get(reso):
            s = pd.concat(series[reso]).sort_index()
            s = s[~s.index.duplicated()]
            if reso != "PT15M":
                n = int(pd.Timedelta(reso.replace("PT", "").lower().replace("m", "min")) / pd.Timedelta(minutes=15))
                s = pd.concat([pd.Series(s.to_numpy(), index=s.index - pd.Timedelta(minutes=15 * k)) for k in range(n)]).sort_index()
            return s
    return pd.Series(dtype=float)


def day_ahead(d: date) -> pd.Series | None:
    """Prix de la journée D (heure de Paris). None si non publiés ou jeton absent."""
    jeton = secret("ENTSOE_TOKEN")
    if not jeton:
        journal("  prix : secret ENTSOE_TOKEN absent, valorisation spot non calculée")
        return None
    a = pd.Timestamp(d, tz=TZ).tz_convert("UTC")
    b = pd.Timestamp(d + timedelta(days=1), tz=TZ).tz_convert("UTC")
    params = {"securityToken": jeton, "documentType": "A44", "in_Domain": ZONE_FR, "out_Domain": ZONE_FR,
              "periodStart": a.strftime("%Y%m%d%H%M"), "periodEnd": b.strftime("%Y%m%d%H%M")}
    for essai in range(3):
        try:
            r = requests.get(URL, params=params, timeout=60)
        except requests.RequestException as e:
            journal(f"  prix : connexion ENTSO-E impossible ({e})")
            return None
        if r.status_code == 200 and "<Publication_MarketDocument" in r.text:
            s = analyser(r.text)
            s = s[(s.index > a) & (s.index <= b)]
            return s if len(s) else None
        if "No matching data" in r.text or r.status_code == 200:
            journal(f"  prix du {d} pas encore publiés")
            return None
        if r.status_code in (429, 503) and essai < 2:
            time.sleep(20)
            continue
        journal(f"  prix : erreur ENTSO-E {r.status_code} {r.text[:200]}")
        return None
    return None
