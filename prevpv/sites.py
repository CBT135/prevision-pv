"""Lecture de la base de données des sites (config/sites.json, écrite par docs/sites.html)."""
from __future__ import annotations

from .commun import FICHIER_SITES, lire_json


def _num(v, defaut=None):
    try:
        return defaut if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return defaut


def charger_sites(actifs_seulement: bool = True) -> list[dict]:
    brut = lire_json(FICHIER_SITES, {"sites": []})
    sites = brut["sites"] if isinstance(brut, dict) else brut
    sortie = []
    for s in sites:
        if actifs_seulement and not s.get("actif", True):
            continue
        manque = verifier(s)
        if manque:
            print(f"  ! site « {s.get('nom', s.get('id'))} » ignoré, paramètres manquants : {', '.join(manque)}")
            continue
        sortie.append(s)
    return sortie


def verifier(s: dict) -> list[str]:
    manque = []
    if _num(s.get("lat")) is None or _num(s.get("lon")) is None:
        manque.append("coordonnées")
    for i, p in enumerate(s.get("pans") or []):
        if not _num(p.get("pc_kwc")):
            manque.append(f"puissance champ {i + 1}")
        if _num(p.get("azimut")) is None:
            manque.append(f"azimut champ {i + 1}")
        if p.get("suivi", "fixe") == "fixe" and _num(p.get("inclinaison")) is None:
            manque.append(f"inclinaison champ {i + 1}")
    if not s.get("pans"):
        manque.append("champs PV")
    if not _num((s.get("onduleur") or {}).get("pac_kva")):
        manque.append("puissance AC")
    return manque


def pc_totale_kwc(s: dict) -> float:
    return sum(_num(p.get("pc_kwc"), 0.0) for p in s["pans"])


num = _num
