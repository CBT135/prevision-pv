"""Mesures déposées en fichiers CSV dans le dépôt.

Production :   data/mesures_depot/<id_site>/*.csv
Consommation : data/conso/<id_site>/*.csv   (pour les modes autoconsommation)

Formats acceptés (séparateur ; ou , — virgule décimale acceptée) :
1. Format du projet : colonnes « horodatage » (fin d'intervalle, heure de Paris ou ISO avec fuseau)
   et « puissance_kw » (puissance moyenne sur l'intervalle).
2. Export de courbe de charge Enedis : colonnes « Horodate » (fin d'intervalle) et « Valeur » en W.
Les pas de 30 ou 60 min sont ramenés au quart d'heure (puissance constante sur l'intervalle).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from ..commun import DOSSIER_CONSO, DOSSIER_DEPOT, TZ, journal


def _lire(chemin: Path) -> pd.Series:
    brut = chemin.read_text(encoding="utf-8-sig", errors="replace")
    sep = ";" if brut.count(";") >= brut.count(",") else ","
    df = pd.read_csv(chemin, sep=sep, dtype=str, encoding="utf-8-sig", encoding_errors="replace")
    cols = {c.lower().strip(): c for c in df.columns}
    if "horodatage" in cols and "puissance_kw" in cols:
        t, v, facteur = cols["horodatage"], cols["puissance_kw"], 1.0
    elif "horodate" in cols and "valeur" in cols:
        t, v, facteur = cols["horodate"], cols["valeur"], 0.001
    else:
        journal(f"  ! {chemin.name} : colonnes non reconnues {list(df.columns)}")
        return pd.Series(dtype=float)
    temps = pd.to_datetime(df[t], utc=False, errors="coerce", format="mixed")
    if temps.dt.tz is None:
        temps = temps.dt.tz_localize(TZ, ambiguous="NaT", nonexistent="NaT")
    temps = temps.dt.tz_convert("UTC")
    val = pd.to_numeric(df[v].str.replace(",", ".", regex=False).str.strip(), errors="coerce") * facteur
    s = pd.Series(val.to_numpy(), index=pd.DatetimeIndex(temps)).dropna()
    s = s[s.index.notna()].sort_index()
    return _au_quart_d_heure(s)


def _au_quart_d_heure(s: pd.Series) -> pd.Series:
    s = s[~s.index.duplicated()]
    if len(s) < 2:
        return s
    pas = s.index.to_series().diff().median()
    if pas <= pd.Timedelta(minutes=15):
        return s
    n = int(round(pas / pd.Timedelta(minutes=15)))
    morceaux = [pd.Series(s.to_numpy(), index=s.index - pd.Timedelta(minutes=15 * k)) for k in range(n)]
    return pd.concat(morceaux).sort_index()


def _dossier(base: Path, site_id: str) -> pd.Series | None:
    d = base / site_id
    fichiers = sorted(d.glob("*.csv")) if d.exists() else []
    if not fichiers:
        return None
    s = pd.concat([_lire(f) for f in fichiers])
    return s[~s.index.duplicated(keep="last")].sort_index()


def production(site: dict, debut: date, fin: date) -> pd.Series | None:
    s = _dossier(DOSSIER_DEPOT, site["id"])
    if s is None:
        return None
    a = pd.Timestamp(debut, tz=TZ).tz_convert("UTC")
    b = (pd.Timestamp(fin, tz=TZ) + pd.Timedelta(days=1)).tz_convert("UTC")
    return s[(s.index > a) & (s.index <= b)]


def consommation(site: dict) -> pd.Series | None:
    return _dossier(DOSSIER_CONSO, site["id"])
