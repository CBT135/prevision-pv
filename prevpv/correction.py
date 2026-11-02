"""Correction statistique apprise sur les mesures.

Pour chaque site, un facteur multiplicatif par état du ciel prévu (indice kt = GHI prévu / GHI ciel clair) :
    couvert (kt < 0,35), mitigé (0,35 – 0,65), clair (kt ≥ 0,65), et un facteur global pour le soleil bas ;
    facteur = Σ production mesurée / Σ production modélisée.
Il corrige à la fois les pertes réelles de la centrale (visibles par ciel clair) et le biais de la
prévision météo par temps couvert (brouillards, stratus), qui n'ont pas la même ampleur.
sur les 30 derniers jours disponibles de data/historique/<site>.csv
(le modèle y est celui calculé avec la prévision émise la veille).

Filtres : quarts d'heure où le modèle dépasse 2 % de la puissance AC ;
quarts d'heure d'arrêt ou de bridage exclus (mesure < 5 % du modèle alors que le modèle dépasse 10 % de Pac) ;
journées exclues si la production mesurée est < 25 % du modèle (panne, arrêt, données absentes).
Facteurs bornés entre 0,3 et 1,3. Il faut au moins 10 jours de données.

Ombrages proches appris selon la position du soleil : par case (azimut 10° × hauteur 4°), rapport
mesuré / modèle des quarts d'heure de ciel dégagé (kt ≥ 0,6), rapporté au rapport moyen de ciel clair.
Une case vue peu de fois est ramenée vers 1 (n / (n + 8)). Le facteur de case ne joue que par temps
dégagé : il est pondéré par l'indice kt prévu (nul sous 0,3, entier au-dessus de 0,65), car un ombrage
ne masque que le direct. Les facteurs par état du ciel sont appris ensuite, sur le modèle déjà corrigé
des ombrages, pour ne pas compter deux fois la même perte. Une case jamais vue (soleil plus bas qu'au
cours des 30 derniers jours) vaut 1.
"""
from __future__ import annotations

import pandas as pd

from .commun import DOSSIER_HISTO, FICHIER_CORRECTION, ecrire_json, lire_json, TZ
from .sites import num

TRANCHES = [(0.0, 0.35), (0.35, 0.65), (0.65, 9.9)]
NOMS_TRANCHES = ["couvert", "mitigé", "clair"]
JOURS = 30
MIN_JOURS = 10
MIN_POINTS = 40
BORNES = (0.3, 1.3)
PAS_AZ, PAS_EL = 10, 4
KT_DEGAGE = 0.6
N0 = 8


COLONNES = ["p_modele_kw", "p_mesure_kw", "elevation", "kt"]


def chemin_histo(site_id: str):
    return DOSSIER_HISTO / f"{site_id}.csv"


def lire_historique(site_id: str) -> pd.DataFrame:
    p = chemin_histo(site_id)
    if not p.exists():
        return pd.DataFrame(columns=COLONNES)
    df = pd.read_csv(p, index_col=0)
    df.index = pd.to_datetime(df.index, utc=True)
    return df


def ajouter_historique(site_id: str, nouveau: pd.DataFrame) -> None:
    df = lire_historique(site_id)
    nouveau = nouveau.reindex(columns=COLONNES)
    df = pd.concat([df[~df.index.isin(nouveau.index)].reindex(columns=COLONNES), nouveau]).sort_index()
    p = chemin_histo(site_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.index.name = "fin_quart_heure_utc"
    df.round(4).to_csv(p)


def _cases(site: dict, index: pd.DatetimeIndex) -> pd.Series:
    from .modele import position_soleil
    sp = position_soleil(site, index)
    az = (sp["azimuth"] // PAS_AZ * PAS_AZ).astype(int).astype(str)
    el = (sp["apparent_elevation"] // PAS_EL * PAS_EL).astype(int).astype(str)
    return az + "|" + el


def _poids_kt(kt: pd.Series) -> pd.Series:
    return ((kt - 0.3) / 0.35).clip(0, 1).fillna(0)


def facteur_position(site: dict, index: pd.DatetimeIndex, kt: pd.Series, c: dict) -> pd.Series:
    pos = (c or {}).get("position") or {}
    fac = pos.get("facteurs") or {}
    if not fac:
        return pd.Series(1.0, index=index)
    f = _cases(site, index).map(fac).astype(float).fillna(1.0)
    return 1 + (f - 1) * _poids_kt(kt.reindex(index))


def apprendre(site: dict) -> dict:
    df = lire_historique(site["id"]).reindex(columns=COLONNES).dropna(subset=["p_modele_kw", "p_mesure_kw", "elevation"])
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

    # 1. ombrages proches selon la position du soleil (ciel dégagé)
    deg = df[df["kt"] >= KT_DEGAGE]
    facteurs = {}
    if len(deg) >= MIN_POINTS:
        ref = deg["p_mesure_kw"].sum() / deg["p_modele_kw"].sum()
        g = deg.groupby(_cases(site, deg.index)).agg(m=("p_mesure_kw", "sum"), p=("p_modele_kw", "sum"),
                                                     n=("p_mesure_kw", "size"))
        for case, r in g.iterrows():
            if r["p"] <= 0 or r["n"] < 3:
                continue
            brut = (r["m"] / r["p"]) / ref
            w = r["n"] / (r["n"] + N0)
            facteurs[case] = round(float(min(max(1 + w * (brut - 1), 0.05), 1.5)), 3)
    res["position"] = {"pas_az": PAS_AZ, "pas_el": PAS_EL, "facteurs": facteurs,
                       "cases_ombrees": sum(1 for v in facteurs.values() if v < 0.8)}
    if facteurs:
        df = df.copy()
        df["p_modele_kw"] = df["p_modele_kw"] * facteur_position(site, df.index, df["kt"], res)

    # 2. facteurs par état du ciel, sur le modèle corrigé des ombrages
    res["global"] = round(ratio(df), 4)
    tr = []
    for lo, hi in TRANCHES:
        d = df[(df["kt"] >= lo) & (df["kt"] < hi)]
        tr.append(round(ratio(d), 4) if len(d) >= MIN_POINTS else res["global"])
    res["tranches"] = tr
    res["noms"] = NOMS_TRANCHES
    res["actif"] = True
    return res


def mettre_a_jour(sites: list[dict]) -> dict:
    tout = lire_json(FICHIER_CORRECTION, {}) or {}
    for s in sites:
        tout[s["id"]] = apprendre(s)
    ecrire_json(FICHIER_CORRECTION, tout)
    return tout


def appliquer(site: dict, modele: pd.DataFrame) -> pd.Series:
    site_id = site["id"] if isinstance(site, dict) else site
    c = (lire_json(FICHIER_CORRECTION, {}) or {}).get(site_id)
    if not c or not c.get("actif"):
        return modele["p_ac_kw"].copy()
    f = pd.Series(c["global"], index=modele.index)
    for (lo, hi), v in zip(TRANCHES, c["tranches"]):
        f[(modele["kt"] >= lo) & (modele["kt"] < hi)] = v
    if isinstance(site, dict):
        f = f * facteur_position(site, modele.index, modele["kt"], c)
    return modele["p_ac_kw"] * f
