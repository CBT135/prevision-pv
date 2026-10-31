"""Point d'entrée.

  python -m prevpv.run quotidien            # mesures de la veille, correction, prix et prévision J+1
  python -m prevpv.run prevision [--date AAAA-MM-JJ]
  python -m prevpv.run historique --jours 90
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, timedelta

import pandas as pd

from . import correction, meteo, mesures, modele, prix, valorisation
from .commun import (DOSSIER_PREV, DOSSIER_REAL, DOSSIER_WEB, TZ, aujourd_hui_paris, ecrire_json, index_journee,
                     journal, lire_json, serie_vers_liste)
from .sites import charger_sites, num, pc_totale_kwc


def _plafond(site: dict, p: pd.Series) -> pd.Series:
    pac = num((site.get("onduleur") or {}).get("pac_kva"), pc_totale_kwc(site))
    lim = num(site.get("limitation_kva")) or pac
    return p.clip(lower=0, upper=min(pac, lim))


def _infos(site: dict) -> dict:
    return {"id": site["id"], "nom": site.get("nom", site["id"]), "type": site.get("type"),
            "pc_kwc": round(pc_totale_kwc(site), 3),
            "pac_kva": num((site.get("onduleur") or {}).get("pac_kva")),
            "mode": (site.get("valorisation") or {}).get("mode")}


# ---------------------------------------------------------------- prévision
def prevision(sites: list[dict], d: date) -> None:
    journal(f"Prévision du {d}")
    idx = index_journee(d)
    px = prix.day_ahead(d)
    sortie = {"date": d.isoformat(), "genere_le": pd.Timestamp.now(tz=TZ).isoformat(timespec="minutes"),
              "pas_fin": [t.tz_convert(TZ).strftime("%H:%M") for t in idx],
              "pas_utc": [t.isoformat() for t in idx],
              "prix_eur_mwh": serie_vers_liste(px.reindex(idx), 2) if px is not None else None,
              "sites": {}}
    for s in sites:
        try:
            m = meteo.previsions(s, d - timedelta(days=1), d + timedelta(days=1)).reindex(idx)
            mod = modele.puissance(s, m)
            p_prev = _plafond(s, correction.appliquer(s["id"], mod))
            conso_prev, src_conso = (None, None)
            if (s.get("valorisation") or {}).get("mode", "").startswith("autoconso"):
                conso_prev, src_conso = valorisation.prevision_conso(mesures.consommation(s), s, idx)
            val = valorisation.calculer(s, p_prev, px, conso_prev)
            corr = (lire_json(correction.FICHIER_CORRECTION, {}) or {}).get(s["id"], {})
            sortie["sites"][s["id"]] = {
                **_infos(s),
                "p_modele_kw": serie_vers_liste(mod["p_ac_kw"]),
                "p_prevue_kw": serie_vers_liste(p_prev),
                "elevation": serie_vers_liste(mod["elevation"], 2),
                "kt": serie_vers_liste(mod["kt"], 3),
                "ghi": serie_vers_liste(m["ghi"], 1),
                "gti_openmeteo": serie_vers_liste(m["gti_openmeteo"], 1),
                "poa": serie_vers_liste(mod["poa"], 1),
                "temp_air": serie_vers_liste(m["temp_air"], 1),
                "vent": serie_vers_liste(m["vent"], 1),
                "nebulosite": serie_vers_liste(m["nebulosite"], 0),
                "conso_prevue_kw": serie_vers_liste(conso_prev) if conso_prev is not None else None,
                "source_conso": src_conso,
                "valorisation": val,
                "correction": {"active": bool(corr.get("actif")), "jours": corr.get("jours", 0),
                               "facteurs": corr.get("tranches")},
            }
            journal(f"  {s['nom']} : {val['energie_kwh']:.1f} kWh, CA {val['ca_eur']} €")
        except Exception as e:  # un site en échec ne bloque pas les autres
            journal(f"  ! {s.get('nom')} : {e}")
            traceback.print_exc()
            sortie["sites"][s["id"]] = {**_infos(s), "erreur": str(e)}
    ecrire_json(DOSSIER_PREV / f"{d.isoformat()}.json", sortie)


# ---------------------------------------------------------------- réalisé
def realise(sites: list[dict], d: date) -> None:
    prev = lire_json(DOSSIER_PREV / f"{d.isoformat()}.json")
    deja = lire_json(DOSSIER_REAL / f"{d.isoformat()}.json", {"sites": {}})
    idx = index_journee(d)
    a_faire = [s for s in sites if s["id"] not in deja.get("sites", {})]
    if not a_faire:
        return
    journal(f"Réalisé du {d}")
    px = None
    if prev and prev.get("prix_eur_mwh"):
        px = pd.Series(prev["prix_eur_mwh"], index=idx, dtype=float)
    elif any((s.get("valorisation") or {}).get("mode") == "spot" for s in a_faire):
        px = prix.day_ahead(d)
    sortie = {"date": d.isoformat(), "genere_le": pd.Timestamp.now(tz=TZ).isoformat(timespec="minutes"),
              "pas_fin": [t.tz_convert(TZ).strftime("%H:%M") for t in idx],
              "prix_eur_mwh": serie_vers_liste(px.reindex(idx), 2) if px is not None else None,
              "sites": dict(deja.get("sites", {}))}
    for s in a_faire:
        try:
            mes = mesures.production(s, d, d)
            if mes is None:
                continue
            mes = mes.reindex(idx)
            if mes.notna().mean() < 0.9:
                journal(f"  {s['nom']} : mesures incomplètes ({mes.notna().mean():.0%}), nouvel essai demain")
                continue
            mes = mes.fillna(0)
            ps = (prev or {}).get("sites", {}).get(s["id"]) or {}
            if "p_prevue_kw" not in ps:
                journal(f"  {s['nom']} : pas de prévision archivée pour cette journée")
                p_prev = p_mod = elev = None
            else:
                p_prev = pd.Series(ps["p_prevue_kw"], index=idx, dtype=float)
                p_mod = pd.Series(ps["p_modele_kw"], index=idx, dtype=float)
                elev = pd.Series(ps["elevation"], index=idx, dtype=float)
                kt = pd.Series(ps.get("kt") or [None] * len(idx), index=idx, dtype=float)
                correction.ajouter_historique(s["id"], pd.DataFrame(
                    {"p_modele_kw": p_mod, "p_mesure_kw": mes, "elevation": elev, "kt": kt}))
            conso = None
            if (s.get("valorisation") or {}).get("mode", "").startswith("autoconso"):
                c = mesures.consommation(s)
                if c is not None and c.reindex(idx).notna().mean() > 0.9:
                    conso = c.reindex(idx)
                else:  # pas de courbe mesurée pour ce jour : même estimation que la prévision
                    conso, _ = valorisation.prevision_conso(c, s, idx)
            val_r = valorisation.calculer(s, mes, px, conso)
            pac = num((s.get("onduleur") or {}).get("pac_kva"), pc_totale_kwc(s))
            res = {**_infos(s), "p_mesure_kw": serie_vers_liste(mes), "valorisation_realisee": val_r,
                   "energie_mesuree_kwh": val_r["energie_kwh"]}
            if p_prev is not None:
                e_prev = float(p_prev.sum() * 0.25)
                jour = elev > 0
                res.update({
                    "p_prevue_kw": serie_vers_liste(p_prev),
                    "energie_prevue_kwh": round(e_prev, 3),
                    "ecart_energie_pct": round((e_prev - val_r["energie_kwh"]) / val_r["energie_kwh"] * 100, 1)
                    if val_r["energie_kwh"] > 0 else None,
                    "mae_kw": round(float((p_prev - mes)[jour].abs().mean()), 4),
                    "nmae_pct": round(float((p_prev - mes)[jour].abs().mean()) / pac * 100, 2),
                    "ca_prevu_eur": ps.get("valorisation", {}).get("ca_eur"),
                })
            sortie["sites"][s["id"]] = res
            journal(f"  {s['nom']} : mesuré {val_r['energie_kwh']:.1f} kWh"
                    + (f", prévu {res['energie_prevue_kwh']:.1f} kWh" if p_prev is not None else ""))
        except Exception as e:
            journal(f"  ! {s.get('nom')} : {e}")
            traceback.print_exc()
    if sortie["sites"]:
        ecrire_json(DOSSIER_REAL / f"{d.isoformat()}.json", sortie)


# ---------------------------------------------------------------- historique
def historique(sites: list[dict], jours: int) -> None:
    fin = aujourd_hui_paris() - timedelta(days=1)
    debut = fin - timedelta(days=jours - 1)
    for s in sites:
        journal(f"Reprise d'historique {s['nom']} du {debut} au {fin}")
        try:
            mes = mesures.production(s, debut, fin)
            if mes is None or mes.empty:
                journal("  aucune mesure disponible")
                continue
            d = debut
            while d <= fin:
                f = min(d + timedelta(days=27), fin)
                idx = pd.date_range(index_journee(d)[0], index_journee(f)[-1], freq="15min")
                m = meteo.previsions(s, d - timedelta(days=1), f + timedelta(days=1), historique=True).reindex(idx)
                mod = modele.puissance(s, m)
                df = pd.DataFrame({"p_modele_kw": mod["p_ac_kw"], "p_mesure_kw": mes.reindex(idx),
                                   "elevation": mod["elevation"], "kt": mod["kt"]}).dropna(subset=["p_mesure_kw", "p_modele_kw"])
                correction.ajouter_historique(s["id"], df)
                journal(f"  {d} → {f} : {len(df)} quarts d'heure")
                d = f + timedelta(days=1)
        except Exception as e:
            journal(f"  ! {e}")
            traceback.print_exc()
    corr = correction.mettre_a_jour(sites)
    for s in sites:
        journal(f"  correction {s['nom']} : {corr.get(s['id'])}")


# ---------------------------------------------------------------- index web
def index_web() -> None:
    prevs = sorted(p.stem for p in DOSSIER_PREV.glob("*.json"))
    reals = sorted(p.stem for p in DOSSIER_REAL.glob("*.json"))
    ecrire_json(DOSSIER_WEB / "index.json", {
        "mis_a_jour": pd.Timestamp.now(tz=TZ).isoformat(timespec="minutes"),
        "previsions": prevs[-60:], "realise": reals[-60:],
        "sites": [_infos(s) for s in charger_sites(actifs_seulement=True)],
    })


def quotidien() -> None:
    sites = charger_sites()
    if not sites:
        journal("Aucun site actif et complet dans config/sites.json")
    t = aujourd_hui_paris()
    for k in (3, 2, 1):
        realise(sites, t - timedelta(days=k))
    correction.mettre_a_jour(sites)
    prevision(sites, t + timedelta(days=1))
    index_web()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="prevpv")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("quotidien")
    p = sub.add_parser("prevision")
    p.add_argument("--date", help="journée à prévoir (défaut : demain)")
    h = sub.add_parser("historique")
    h.add_argument("--jours", type=int, default=60)
    a = ap.parse_args(argv)
    if a.cmd == "quotidien":
        quotidien()
    elif a.cmd == "prevision":
        d = date.fromisoformat(a.date) if a.date else aujourd_hui_paris() + timedelta(days=1)
        prevision(charger_sites(), d)
        index_web()
    elif a.cmd == "historique":
        historique(charger_sites(), a.jours)
    return 0


if __name__ == "__main__":
    sys.exit(main())
