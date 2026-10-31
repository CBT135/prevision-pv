"""Valorisation de l'énergie produite sur une journée, selon le mode choisi dans la base des sites.

Modes :
- autoconso_sans_surplus : autoconsommé × prix d'achat évité ; le surplus n'est pas valorisé.
- autoconso_surplus      : autoconsommé × prix évité + surplus injecté × tarif de rachat.
- vente_totale           : production × tarif (€/MWh).
- spot                   : Σ production¼h × (prix day-ahead¼h − décote) ;
                           option « arrêt » : injection nulle sur les quarts d'heure à prix négatif ;
                           option complément de rémunération : (tarif de référence − prix moyen pondéré
                           par la production) × énergie, estimation journalière indicative
                           (le calcul contractuel est mensuel, avec son propre M0).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .commun import TZ
from .sites import num


def prevision_conso(conso: pd.Series | None, site: dict, index: pd.DatetimeIndex) -> tuple[pd.Series | None, str]:
    """Consommation prévue (kW) : moyenne des 4 mêmes jours de semaine précédents, sinon profil plat annuel."""
    if conso is not None and len(conso):
        local = conso.tz_convert(TZ)
        cible = index.tz_convert(TZ)
        profils = []
        for k in range(1, 5):
            s = conso.reindex(index - pd.Timedelta(days=7 * k))
            if s.notna().mean() > 0.9:
                profils.append(s.to_numpy())
        if profils:
            return pd.Series(np.nanmean(profils, axis=0), index=index), f"moyenne des {len(profils)} mêmes jours précédents"
        recents = local[local.index > local.index.max() - pd.Timedelta(days=14)]
        if len(recents) > 96 * 3:
            prof = recents.groupby([recents.index.hour, recents.index.minute]).mean()
            vals = [prof.get((t.hour, t.minute), float("nan")) for t in cible]
            return pd.Series(vals, index=index), "profil moyen des 14 derniers jours"
    annuelle = num(((site.get("valorisation") or {}).get("conso") or {}).get("annuelle_kwh"))
    if annuelle:
        return pd.Series(annuelle / 8760.0, index=index), "profil plat (consommation annuelle)"
    return None, "consommation inconnue"


def calculer(site: dict, prod_kw: pd.Series, prix: pd.Series | None, conso_kw: pd.Series | None) -> dict:
    v = site.get("valorisation") or {}
    mode = v.get("mode", "vente_totale")
    e = prod_kw.clip(lower=0) * 0.25  # kWh par quart d'heure
    r = {"mode": mode, "energie_kwh": round(float(e.sum()), 3), "ca_eur": None, "detail": {}}

    if mode.startswith("autoconso"):
        prix_achat = num(v.get("prix_achat_eur_kwh"))
        manque = []
        if prix_achat is None:
            manque.append("prix d'achat évité")
        if conso_kw is None:
            manque.append("courbe de consommation (CSV Enedis dans data/conso/<id du site>/ ou consommation annuelle)")
        if manque:
            r["detail"]["manque"] = " et ".join(manque)
            return r
        c = conso_kw.reindex(prod_kw.index).fillna(0).clip(lower=0) * 0.25
        auto = pd.concat([e, c], axis=1).min(axis=1)
        surplus = (e - auto).clip(lower=0)
        ca = float(auto.sum()) * prix_achat
        r["detail"].update(autoconsomme_kwh=round(float(auto.sum()), 3), surplus_kwh=round(float(surplus.sum()), 3),
                           taux_autoconso=round(float(auto.sum() / e.sum()), 4) if e.sum() > 0 else None)
        if mode == "autoconso_surplus":
            tarif = num(v.get("tarif_surplus_eur_kwh"), 0.0)
            ca += float(surplus.sum()) * tarif
            r["detail"]["vente_surplus_eur"] = round(float(surplus.sum()) * tarif, 2)
        r["ca_eur"] = round(ca, 2)
        return r

    if mode == "vente_totale":
        tarif = num(v.get("tarif_vente_eur_mwh"))
        if tarif is None:
            r["detail"]["manque"] = "tarif de vente"
            return r
        r["ca_eur"] = round(float(e.sum()) / 1000.0 * tarif, 2)
        return r

    # spot
    sp = v.get("spot") or {}
    if prix is None or prix.reindex(prod_kw.index).isna().all():
        r["detail"]["manque"] = "prix day-ahead non publiés"
        return r
    p = prix.reindex(prod_kw.index)
    decote = num(sp.get("decote_eur_mwh"), 0.0)
    injecte = e.copy()
    arret = pd.Series(False, index=e.index)
    if sp.get("prix_negatif", "arret") == "arret":
        arret = (p < 0) & (e > 0)
        injecte[arret] = 0.0
    marche = float((injecte / 1000.0 * (p - decote)).sum())
    r["detail"].update(
        marche_eur=round(marche, 2),
        quarts_prix_negatif=int((p < 0).sum()),
        energie_arretee_kwh=round(float(e[arret].sum()), 3),
        prix_moyen_pondere=round(float((injecte * p).sum() / injecte.sum()), 2) if injecte.sum() > 0 else None,
        prix_moyen_base=round(float(p.mean()), 2),
    )
    ca = marche
    if sp.get("complement") and num(sp.get("prix_reference_eur_mwh")) is not None and injecte.sum() > 0:
        ref = num(sp.get("prix_reference_eur_mwh"))
        comp = (ref - r["detail"]["prix_moyen_pondere"]) * float(injecte.sum()) / 1000.0
        r["detail"]["complement_estime_eur"] = round(comp, 2)
        ca += comp
    r["ca_eur"] = round(ca, 2)
    return r
