"""Prévisions météo Open-Meteo (modèles Météo-France ARPEGE/AROME) au pas 15 min.

Variables récupérées (moyennes sur le quart d'heure précédent pour le rayonnement) :
GHI, DHI, DNI, température à 2 m, vent à 10 m, et la GTI du premier champ comme contrôle.
La nébulosité n'existe qu'au pas horaire : elle est interpolée.

Au-delà de ~6 h d'échéance, le pas 15 min est interpolé par Open-Meteo depuis le pas horaire.
"""
from __future__ import annotations

import time
from datetime import date

import pandas as pd
import requests

from .commun import journal, secret

VARS_15 = [
    "shortwave_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "global_tilted_irradiance",
    "temperature_2m",
    "wind_speed_10m",
]
RENOMMER = {
    "shortwave_radiation": "ghi",
    "diffuse_radiation": "dhi",
    "direct_normal_irradiance": "dni",
    "global_tilted_irradiance": "gti_openmeteo",
    "temperature_2m": "temp_air",
    "wind_speed_10m": "vent",
}
MODELE = "meteofrance_seamless"


def _base(historique: bool) -> str:
    cle = secret("OPEN_METEO_API_KEY")
    if historique:
        return ("https://customer-historical-forecast-api.open-meteo.com" if cle else
                "https://historical-forecast-api.open-meteo.com") + "/v1/forecast"
    return ("https://customer-api.open-meteo.com" if cle else "https://api.open-meteo.com") + "/v1/forecast"


def _get(url: str, params: dict) -> dict:
    cle = secret("OPEN_METEO_API_KEY")
    if cle:
        params = dict(params, apikey=cle)
    for essai in range(3):
        r = requests.get(url, params=params, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and essai < 2:
            time.sleep(10 * (essai + 1))
            continue
        raise RuntimeError(f"Open-Meteo {r.status_code} : {r.text[:300]}")
    raise RuntimeError("Open-Meteo indisponible")


def previsions(site: dict, debut: date, fin: date, historique: bool = False) -> pd.DataFrame:
    """DataFrame indexé par fin de quart d'heure (UTC), de debut 00:00 à fin 24:00 UTC (marge incluse)."""
    pan0 = site["pans"][0]
    params = {
        "latitude": float(site["lat"]),
        "longitude": float(site["lon"]),
        "models": MODELE,
        "minutely_15": ",".join(VARS_15),
        "hourly": "cloud_cover",
        "timezone": "UTC",
        "wind_speed_unit": "ms",
        "tilt": float(pan0.get("inclinaison") or 0),
        "azimuth": float(pan0.get("azimut") or 0),
        "start_date": debut.isoformat(),
        "end_date": fin.isoformat(),
    }
    j = _get(_base(historique), params)
    m15 = j.get("minutely_15") or {}
    df = pd.DataFrame({k: m15.get(k) for k in VARS_15}, index=pd.to_datetime(m15.get("time", []), utc=True))
    df = df.rename(columns=RENOMMER).astype(float)

    if df.empty or df["ghi"].isna().all():
        journal("  pas 15 min indisponible, repli sur le pas horaire")
        df = _repli_horaire(params, historique)

    h = j.get("hourly") or {}
    if h.get("time"):
        cc = pd.Series(h["cloud_cover"], index=pd.to_datetime(h["time"], utc=True), dtype=float)
        df["nebulosite"] = cc.reindex(df.index.union(cc.index)).interpolate("time").reindex(df.index)
    else:
        df["nebulosite"] = float("nan")
    return df


def _repli_horaire(params: dict, historique: bool) -> pd.DataFrame:
    p = {k: v for k, v in params.items() if k != "minutely_15"}
    p["hourly"] = ",".join(VARS_15)
    j = _get(_base(historique), p)
    h = j["hourly"]
    df = pd.DataFrame({k: h.get(k) for k in VARS_15}, index=pd.to_datetime(h["time"], utc=True))
    df = df.rename(columns=RENOMMER).astype(float)
    # Le rayonnement horaire est la moyenne de l'heure précédente : chaque quart d'heure de cette heure reçoit la moyenne.
    idx = pd.date_range(df.index[0] - pd.Timedelta(minutes=45), df.index[-1], freq="15min")
    ray = df[["ghi", "dhi", "dni", "gti_openmeteo"]].reindex(idx).bfill()
    inst = df[["temp_air", "vent"]].reindex(idx).interpolate("time").bfill()
    return pd.concat([ray, inst], axis=1)
