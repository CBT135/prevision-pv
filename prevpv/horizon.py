"""Masque d'horizon PVGIS (Commission européenne, JRC).

Un site dont le masque vaut {"source": "pvgis"} sans points reçoit, au passage suivant du calcul,
le profil d'horizon calculé par PVGIS à partir du relief (modèle numérique de terrain).
PVGIS n'accepte pas les appels depuis un navigateur : l'import se fait ici, côté GitHub Actions.
Convention des azimuts PVGIS : 0 = Sud, −90 = Est, +90 = Ouest (identique à PVsyst).
"""
from __future__ import annotations

import json

import pandas as pd
import requests

from .commun import FICHIER_SITES, TZ, journal

URL = "https://re.jrc.ec.europa.eu/api/v5_3/printhorizon"


def profil(lat: float, lon: float) -> list[list[float]]:
    r = requests.get(URL, params={"lat": lat, "lon": lon, "outputformat": "json"}, timeout=60)
    r.raise_for_status()
    pts = r.json()["outputs"]["horizon_profile"]
    out = sorted({round(float(p["A"]), 1): round(float(p["H_hor"]), 1) for p in pts}.items())
    return [[a, h] for a, h in out]


def completer() -> bool:
    """Importe les masques PVGIS en attente dans config/sites.json. Retourne True si le fichier a changé."""
    brut = json.loads(FICHIER_SITES.read_text(encoding="utf-8"))
    sites = brut["sites"] if isinstance(brut, dict) else brut
    change = False
    for s in sites:
        m = s.get("masque") or {}
        if m.get("source") != "pvgis" or m.get("points"):
            continue
        try:
            pts = profil(float(s["lat"]), float(s["lon"]))
        except Exception as e:  # PVGIS indisponible : nouvel essai au prochain passage
            journal(f"  ! masque PVGIS de {s.get('nom')} : {e}")
            continue
        s["masque"] = {"source": "pvgis", "fichier": "PVGIS (relief, JRC)", "convention": "pvsyst_sud0",
                       "importe_le": pd.Timestamp.now(tz=TZ).date().isoformat(), "points": pts}
        journal(f"  masque PVGIS importé pour {s.get('nom')} : {len(pts)} points, "
                f"hauteur max {max(p[1] for p in pts):.1f}°")
        change = True
    if change:
        FICHIER_SITES.write_text(json.dumps(brut, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return change
