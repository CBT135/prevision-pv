"""Sources de mesures de production (et de consommation) au pas 15 min."""
from __future__ import annotations

from datetime import date

import pandas as pd

from . import depot, enphase


def production(site: dict, debut: date, fin: date) -> pd.Series | None:
    """Puissance moyenne mesurée (kW) par fin de quart d'heure UTC, journées debut..fin incluses (heure de Paris)."""
    source = (site.get("mesures") or {}).get("source", "csv")
    if source == "enphase":
        return enphase.production(site, debut, fin)
    return depot.production(site, debut, fin)


def consommation(site: dict) -> pd.Series | None:
    """Courbe du compteur de consommation (soutirage réseau) déposée, en kW."""
    return depot.consommation(site)


def consommation_totale(site: dict) -> pd.Series | None:
    """Consommation totale du site = compteur (soutirage) + production PV mesurée, en kW.
    Définie seulement là où la production mesurée est connue (historique du site)."""
    from ..correction import lire_historique
    sout = depot.consommation(site)
    if sout is None:
        return None
    prod = lire_historique(site["id"])["p_mesure_kw"].astype(float).clip(lower=0)
    return (sout + prod.reindex(sout.index)).dropna()
