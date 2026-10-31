"""Correction statistique apprise sur les mesures.

Pour chaque site, un facteur multiplicatif par tranche d'élévation solaire :
    facteur = Σ production mesurée / Σ production modélisée
sur les 30 derniers jours disponibles de data/historique/<site>.csv
(le modèle y est celui calculé avec la prévision émise la veille).

Filtres : quarts d'heure où le modèle dépasse 2 % de la puissance AC ;
quarts d'heure d'arrêt ou de bridage exclus (mesure < 5 % du modèle alors que le modèle dépasse 10 % de Pac) ;
journées exclues si la production mesurée est < 25 % du modèle (panne, arrêt, données absentes).
Facteurs bornés entre 0,6 et 1,3. Il faut au moins 10 jours de données.
"""
from __future__ import annotations

import pandas as pd

from .commun import DOSSIER_HISTO, FICHIER_CORRECTION, ecrire_json, lire_json, TZ
from .sites import num

TRANCHES = [(-1, 15), (15, 35), (35, 91)]
JOURS = 30
MIN_JOURS = 10
MIN_POINTS = 40
BORNES = (0.6, 1.3)


def chemin_histo(site_id: str):
    return DOSSIER_HISTO / f"{site_id}.csv"


def lire_historique(site_id: str) -> pd.DataFrame:
    p = chemin_histo(site_id)
    if not p.exists():
        return pd.DataFrame(columns=["p_modele_kw", "p_mesure_kw", "elevation"])
    df = pd.read_csv(p, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def ajouter_historique(site_id: str, nouveau: pd.DataFrame) -> None:
    df = lire_historique(site_id)
    df = pd.concat([df[~df.index.isin(nouveau.index)], nouveau[["p_modele_kw", "p_mesure_kw", "elevation"]]]).sort_index()
    p = chemin_histo(site_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.index.name = "fin_quart_heure_utc"
    df.round(4).to_csv(p)


def apprendre(site: dict) -> dict:
    df = lire_historique(site["id"]).dropna()
    res = {"jours": 0, "global": 1.0, "tranches": [1.0] * len(TRANCHES), "actif": False}
    if df.empty:
        return res
    fin = df.index.max()
    df = df[df.index > fin - pd.Timedelta(days=JOURS)]
    jour = df.index.tz_convert(TZ).date
    e = df.groupby(jour)[["p_modele_kw", "p_mesure_kw"]].sum()
    jours_ok = e.index[(e["p_modele_kw"] > 0) & (e["p_mesure_kw"] >= 0.25 * e["p_modele_kw"])]
    df = df[pd.Index(jour).isin(jours_ok)]
    pac = num((site.get("onduleur") or {}).get("pac_kva"), 1.0)
    df = df[df["p_modele_kw"] > 0.02 * pac]
    arret = (df["p_mesure_kw"] < 0.05 * df["p_modele_kw"]) & (df["p_modele_kw"] > 0.10 * pac)
    res["quarts_arret_exclus"] = int(arret.sum())
    df = df[~arret]
    res["jours"] = len(jours_ok)
    if len(jours_ok) < MIN_JOURS or df.empty:
        return res

    def ratio(d):
        return min(max(d["p_mesure_kw"].sum() / d["p_modele_kw"].sum(), BORNES[0]), BORNES[1])

    res["global"] = round(ratio(df), 4)
    tr = []
    for lo, hi in TRANCHES:
        d = df[(df["elevation"] >= lo) & (df["elevation"] < hi)]
        tr.append(round(ratio(d), 4) if len(d) >= MIN_POINTS else res["global"])
    res["tranches"] = tr
    res["actif"] = True
    return res


def mettre_a_jour(sites: list[dict]) -> dict:
    tout = lire_json(FICHIER_CORRECTION, {}) or {}
    for s in sites:
        tout[s["id"]] = apprendre(s)
    ecrire_json(FICHIER_CORRECTION, tout)
    return tout


def appliquer(site_id: str, modele: pd.DataFrame) -> pd.Series:
    c = (lire_json(FICHIER_CORRECTION, {}) or {}).get(site_id)
    if not c or not c.get("actif"):
        return modele["p_ac_kw"].copy()
    f = pd.Series(c["global"], index=modele.index)
    for (lo, hi), v in zip(TRANCHES, c["tranches"]):
        f[(modele["elevation"] >= lo) & (modele["elevation"] < hi)] = v
    return modele["p_ac_kw"] * f
