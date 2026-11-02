"""Courbes de charge Enedis (compteurs Linky ≤ 36 kVA) via la passerelle MyElectricalData.

Un jeton par PRM, obtenu sur myelectricaldata.fr après consentement sur le compte Enedis du titulaire,
stocké dans un secret du dépôt ENEDIS_JETON_01 … ENEDIS_JETON_20, écrit par la page « Base de données des sites »
qui attribue l'emplacement et le note dans la fiche du site (champ jeton_secret, le nom seulement).
Les noms de secrets sont fixes pour que le workflow les déclare explicitement.

- consommation : PRM valorisation.conso.prm (modes autoconsommation, source « enedis ») -> data/conso/<id>/
- production   : PRM mesures.prm_production (source des mesures « enedis »)              -> data/mesures_depot/<id>/

API : GET https://www.myelectricaldata.fr/<consumption|production>_load_curve/<PRM>/start/<J>/end/<J'>
(fin exclue, 7 jours au plus), jeton brut dans l'en-tête Authorization. Chaque valeur est une puissance
moyenne en W ; « date » est la FIN de l'intervalle, heure de Paris ; pas de 10, 30 ou 60 min.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from ..commun import DOSSIER_CONSO, DOSSIER_DEPOT, TZ, aujourd_hui_paris, journal, secret

BASE = "https://www.myelectricaldata.fr"
JOURS_REPRISE = 60  # historique récupéré au premier passage


def nom_secret(prm: str) -> str:
    return "ENEDIS_" + "".join(c for c in str(prm) if c.isalnum())


def _appel(sens: str, prm: str, jeton: str, debut: date, fin: date, cache: bool) -> pd.Series:
    url = f"{BASE}/{sens}_load_curve/{prm}/start/{debut}/end/{fin}" + ("/cache" if cache else "")
    for essai in range(3):
        r = requests.get(url, headers={"Authorization": jeton, "Accept": "application/json"}, timeout=60)
        if r.status_code in (429, 500, 502, 503, 504) and essai < 2:
            time.sleep(5 * (essai + 1))
            continue
        break
    if r.status_code == 404:  # Enedis ne détient rien sur la période
        return pd.Series(dtype=float)
    if r.status_code in (401, 403):
        raise RuntimeError(f"jeton refusé ({r.status_code}) : consentement expiré ou jeton à renouveler sur myelectricaldata.fr")
    r.raise_for_status()
    lectures = ((r.json() or {}).get("meter_reading") or {}).get("interval_reading") or []
    fins, vals, prec = [], [], None
    for it in lectures:
        t = pd.Timestamp(datetime.strptime(str(it["date"]), "%Y-%m-%d %H:%M:%S"))
        loc = t.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")  # heure d'été d'abord
        if prec is not None and loc <= prec:  # heure répétée au passage à l'heure d'hiver
            loc = t.tz_localize(TZ, ambiguous=False, nonexistent="shift_forward")
            if loc <= prec:
                continue
        prec = loc
        fins.append(loc.tz_convert("UTC"))
        vals.append(float(it["value"]) / 1000.0)
    s = pd.Series(vals, index=pd.DatetimeIndex(fins))
    return s[~s.index.duplicated()].sort_index()


def _dossier(sens: str, site_id: str) -> Path:
    return (DOSSIER_CONSO if sens == "consumption" else DOSSIER_DEPOT) / site_id


def _lire_existant(d: Path) -> pd.Series:
    parts = []
    for f in sorted(d.glob("enedis_*.csv")):
        df = pd.read_csv(f, sep=";")
        parts.append(pd.Series(df["puissance_kw"].to_numpy(dtype=float),
                               index=pd.to_datetime(df["horodatage"], utc=True)))
    return pd.concat(parts).sort_index() if parts else pd.Series(dtype=float)


def _ecrire(d: Path, s: pd.Series) -> None:
    d.mkdir(parents=True, exist_ok=True)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    loc = s.index.tz_convert(TZ)
    for mois, part in s.groupby(loc.strftime("%Y-%m")):
        df = pd.DataFrame({"horodatage": part.index.map(lambda t: t.isoformat()),  # UTC, fin d'intervalle
                           "puissance_kw": part.round(4).to_numpy()})
        df.to_csv(d / f"enedis_{mois}.csv", sep=";", index=False)


def actualiser(site: dict, sens: str, prm: str, nom: str | None = None) -> None:
    nom = nom or nom_secret(prm)
    jeton = secret(nom)
    if not jeton:
        journal(f"  {site.get('nom')} : secret {nom} vide ou absent, courbe Enedis non récupérée")
        return
    d = _dossier(sens, site["id"])
    existant = _lire_existant(d)
    fin = aujourd_hui_paris()  # exclu : la veille est la dernière journée disponible
    debut = fin - timedelta(days=JOURS_REPRISE)
    if len(existant):
        derniere = existant.index.max().tz_convert(TZ).date()
        debut = max(debut, derniere - timedelta(days=2))  # Enedis complète parfois avec retard
    morceaux, d0 = [], debut
    while d0 < fin:
        d1 = min(d0 + timedelta(days=7), fin)
        morceaux.append(_appel(sens, prm, jeton, d0, d1, cache=d1 < fin - timedelta(days=3)))
        d0 = d1
    nouveau = pd.concat(morceaux) if morceaux else pd.Series(dtype=float)
    if nouveau.empty:
        journal(f"  {site.get('nom')} : aucune courbe Enedis {sens} du {debut} au {fin - timedelta(days=1)}")
        return
    _ecrire(d, pd.concat([existant[~existant.index.isin(nouveau.index)], nouveau]))
    journal(f"  {site.get('nom')} : courbe Enedis {'consommation' if sens == 'consumption' else 'production'} "
            f"à jour jusqu'au {nouveau.index.max().tz_convert(TZ):%d/%m %H:%M} ({len(nouveau)} pas récupérés)")


def actualiser_tous(sites: list[dict]) -> None:
    for s in sites:
        conso = (s.get("valorisation") or {}).get("conso") or {}
        mes = s.get("mesures") or {}
        travaux = []
        if str((s.get("valorisation") or {}).get("mode", "")).startswith("autoconso") \
                and conso.get("source") == "enedis" and conso.get("prm"):
            travaux.append(("consumption", conso["prm"], conso.get("jeton_secret")))
        if mes.get("source") == "enedis" and mes.get("prm_production"):
            travaux.append(("production", mes["prm_production"], mes.get("jeton_secret")))
        for sens, prm, nom in travaux:
            try:
                actualiser(s, sens, str(prm).strip(), nom)
            except Exception as e:  # un PRM en échec ne bloque pas le calcul
                journal(f"  ! {s.get('nom')} : courbe Enedis {sens} : {e}")
