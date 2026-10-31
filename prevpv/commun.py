"""Chemins, fuseaux horaires et outils partagés.

Conventions du projet
- Tous les horodatages internes sont en UTC et désignent la FIN du quart d'heure
  (convention Enedis / Enphase / Open-Meteo pour les moyennes sur intervalle).
- Une « journée » D est la journée civile à Paris : quarts d'heure finissant
  entre D 00:15 et D+1 00:00 heure de Paris (96 pas, 92 ou 100 aux changements d'heure).
- Puissances en kW, énergies en kWh, prix en €/MWh (sauf tarifs résidentiels en €/kWh).
- Azimuts en convention PVsyst / Open-Meteo : 0 = Sud, -90 = Est, +90 = Ouest.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

RACINE = Path(__file__).resolve().parent.parent
FICHIER_SITES = RACINE / "config" / "sites.json"
DOSSIER_DATA = RACINE / "data"
DOSSIER_HISTO = DOSSIER_DATA / "historique"
DOSSIER_CONSO = DOSSIER_DATA / "conso"
DOSSIER_DEPOT = DOSSIER_DATA / "mesures_depot"
FICHIER_CORRECTION = DOSSIER_DATA / "correction.json"
DOSSIER_WEB = RACINE / "docs" / "data"
DOSSIER_PREV = DOSSIER_WEB / "previsions"
DOSSIER_REAL = DOSSIER_WEB / "realise"

TZ = "Europe/Paris"
PAS = pd.Timedelta(minutes=15)


def aujourd_hui_paris() -> date:
    return pd.Timestamp.now(tz=TZ).date()


def index_journee(d: date) -> pd.DatetimeIndex:
    """Fins de quarts d'heure de la journée D (heure de Paris), exprimées en UTC."""
    debut = pd.Timestamp(d, tz=TZ)
    fin = pd.Timestamp(d + timedelta(days=1), tz=TZ)
    return pd.date_range(debut + PAS, fin, freq=PAS).tz_convert("UTC")


def lire_json(chemin: Path, defaut=None):
    try:
        return json.loads(Path(chemin).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return defaut


def ecrire_json(chemin: Path, donnees) -> None:
    chemin = Path(chemin)
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(donnees, ensure_ascii=False, indent=1, default=_json_defaut) + "\n", encoding="utf-8")


def _json_defaut(o):
    if isinstance(o, (pd.Timestamp, datetime, date)):
        return o.isoformat()
    if hasattr(o, "item"):
        return o.item()
    raise TypeError(type(o))


def serie_vers_liste(s: pd.Series, decimales: int = 3) -> list:
    return [None if pd.isna(v) else round(float(v), decimales) for v in s.to_numpy()]


def journal(msg: str) -> None:
    print(f"[{pd.Timestamp.now(tz=TZ):%H:%M:%S}] {msg}", flush=True)


def secret(nom: str) -> str | None:
    v = os.environ.get(nom, "").strip()
    return v or None
