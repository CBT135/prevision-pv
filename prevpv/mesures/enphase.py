"""API Enphase Enlighten v4 (plan gratuit « Watt » : 10 appels/min, 1 000 appels/mois).

Secrets attendus (GitHub > Settings > Secrets and variables > Actions) :
  ENPHASE_API_KEY, ENPHASE_CLIENT_ID, ENPHASE_CLIENT_SECRET, ENPHASE_REFRESH_TOKEN

À chaque exécution, le jeton de rafraîchissement est échangé contre un jeton d'accès.
Si Enphase renvoie un nouveau jeton de rafraîchissement, il est écrit dans le fichier
désigné par ENPHASE_REFRESH_TOKEN_SORTIE ; le workflow le réenregistre dans les secrets.

Consommation d'appels : 1 appel « token » + 1 appel par site et par jour (granularité jour, 96 valeurs).
La reprise d'historique utilise la granularité semaine (7 jours par appel).
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta

import pandas as pd
import requests

from ..commun import TZ, journal, secret

BASE = "https://api.enphaseenergy.com"
_jeton = {"acces": None}


class EnphaseErreur(RuntimeError):
    pass


def _jeton_acces() -> str:
    if _jeton["acces"]:
        return _jeton["acces"]
    cid, csec, rt = secret("ENPHASE_CLIENT_ID"), secret("ENPHASE_CLIENT_SECRET"), secret("ENPHASE_REFRESH_TOKEN")
    if not (cid and csec and rt):
        raise EnphaseErreur("secrets ENPHASE_CLIENT_ID / ENPHASE_CLIENT_SECRET / ENPHASE_REFRESH_TOKEN manquants")
    r = requests.post(f"{BASE}/oauth/token", params={"grant_type": "refresh_token", "refresh_token": rt},
                      auth=(cid, csec), timeout=30)
    if r.status_code != 200:
        raise EnphaseErreur(f"rafraîchissement du jeton refusé ({r.status_code}) : {r.text[:200]}")
    j = r.json()
    _jeton["acces"] = j["access_token"]
    nouveau = j.get("refresh_token")
    sortie = os.environ.get("ENPHASE_REFRESH_TOKEN_SORTIE")
    if nouveau and nouveau != rt and sortie:
        with open(sortie, "w") as f:
            f.write(nouveau)
        journal("  nouveau jeton de rafraîchissement Enphase reçu (sera réenregistré)")
    return _jeton["acces"]


def _get(chemin: str, params: dict) -> dict:
    cle = secret("ENPHASE_API_KEY")
    if not cle:
        raise EnphaseErreur("secret ENPHASE_API_KEY manquant")
    for essai in range(3):
        r = requests.get(f"{BASE}/api/v4{chemin}", params=dict(params, key=cle),
                         headers={"Authorization": f"Bearer {_jeton_acces()}"}, timeout=60)
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429 and essai < 2:
            time.sleep(65)
            continue
        raise EnphaseErreur(f"API Enphase {r.status_code} sur {chemin} : {r.text[:200]}")
    raise EnphaseErreur("quota Enphase dépassé")


def _intervalles(system_id: str, debut_jour: date, granularite: str) -> pd.Series:
    debut = int(pd.Timestamp(debut_jour, tz=TZ).timestamp())
    j = _get(f"/systems/{system_id}/telemetry/production_micro", {"start_at": debut, "granularity": granularite})
    iv = j.get("intervals") or []
    if not iv:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([i["end_at"] for i in iv], unit="s", utc=True)
    # enwh = énergie du quart d'heure (Wh) -> puissance moyenne en kW
    val = [(i.get("enwh") if i.get("enwh") is not None else (i.get("powr") or 0) / 4) / 250.0 for i in iv]
    return pd.Series(val, index=idx, dtype=float)


def production(site: dict, debut: date, fin: date) -> pd.Series | None:
    sid = str((site.get("mesures") or {}).get("enphase_system_id") or "").strip()
    if not sid:
        return None
    morceaux = []
    nb_jours = (fin - debut).days + 1
    if nb_jours <= 2:
        d = debut
        while d <= fin:
            morceaux.append(_intervalles(sid, d, "day"))
            d += timedelta(days=1)
    else:
        d = debut
        while d <= fin:
            morceaux.append(_intervalles(sid, d, "week"))
            d += timedelta(days=7)
            time.sleep(7)  # 10 appels/min maximum
    if not morceaux:
        return None
    s = pd.concat(morceaux)
    s = s[~s.index.duplicated()].sort_index()
    return s
