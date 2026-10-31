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
    return depot.consommation(site)
