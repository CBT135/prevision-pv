"""Courbes de charge Enedis (compteurs Linky ≤ 36 kVA) via la passerelle MyElectricalData.

Un jeton par PRM, obtenu sur myelectricaldata.fr après consentement sur le compte Enedis du titulaire,
stocké dans un secret du dépôt ENEDIS_JETON_01 … ENEDIS_JETON_20, écrit par la page « Base de données des sites »
qui attribue l'emplacement et le note dans la fiche du site (champ jeton_secret, le nom seulement).
Les noms de secrets sont fixes pour que le workflow les déclare explicitement.

- consommation : PRM valorisation.conso.prm (modes autoconsommation, source « enedis ») -> data/conso/<id>/
- production   : PRM mesures.prm_production (source des mesures « enedis »)              -> data/mesures_depot/<id>/

API : GET https://www.myelectricaldata.fr/<consumption|production>_load_curve/<PRM>/start/<J>/end/<J'>
(fin exclue, 7 jours au plus), jeton brut dans l'en-tête Authorization. Chaque valeur est une puissance
moyenne en W ; « date » est la FIN de l'intervalle, heure de Paris ; pas de 10, 30 ou 60 min.

Quota de la passerelle (par jeton) : 500 appels / jour avec le cache chiffré de la passerelle (suffixe
/cache), 50 sans ; Enedis : 5 appels / seconde au plus. D'où :
- tous les appels passent par /cache ;
- une seule tentative par PRM et par jour, au premier passage après 7 h (heure de Paris), et une
  seconde l'après-midi seulement si la veille n'était pas encore publiée le matin ;
- pas de nouvel essai sur un refus de quota (429), qui compterait lui aussi ;
- au moins 0,3 s entre deux appels.
Les tentatives du jour sont notées dans data/enedis_appels.json (publié avec les résultats).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from ..commun import (DOSSIER_CONSO, DOSSIER_DATA, DOSSIER_DEPOT, TZ, aujourd_hui_paris, ecrire_json, journal,
                      lire_json, secret)

BASE = "https://www.myelectricaldata.fr"
JOURS_REPRISE = 60  # historique récupéré au premier passage
HEURE_PREMIERE = 7   # heure de Paris : première tentative du jour pour la veille
HEURE_SECONDE = 14   # seconde tentative si la veille manquait encore le matin
ECART_APPELS_S = 0.3  # Enedis : 5 appels / seconde au plus
FICHIER_APPELS = DOSSIER_DATA / "enedis_appels.json"
_dernier_appel = [0.0]


def _get(url: str, jeton: str) -> requests.Response:
    attente = ECART_APPELS_S - (time.monotonic() - _dernier_appel[0])
    if attente > 0:
        time.sleep(attente)
    try:
        return requests.get(url, headers={"Authorization": jeton, "Accept": "application/json"}, timeout=60)
    finally:
        _dernier_appel[0] = time.monotonic()


_DIAG: list[str] = []  # réponses brutes de la passerelle, journalisées quand rien n'est récupéré


def _diagnostic(sens: str, prm: str, jeton: str) -> None:
    """Rien reçu : réponses obtenues, et contrôle du jeton sur les données journalières et le contrat."""
    for l in _DIAG[-3:]:
        journal(f"    réponse courbe {l}")
    fin = aujourd_hui_paris()
    for chemin in (f"daily_{sens}/{prm}/start/{fin - timedelta(days=7)}/end/{fin}", f"contracts/{prm}/cache"):
        try:
            r = _get(f"{BASE}/{chemin}/cache" if not chemin.endswith("/cache") else f"{BASE}/{chemin}", jeton)
            journal(f"    contrôle {chemin.split('/')[0]} : HTTP {r.status_code} {r.text[:200]!r}")
        except Exception as e:
            journal(f"    contrôle {chemin.split('/')[0]} : {e}")


def nom_secret(prm: str) -> str:
    return "ENEDIS_" + "".join(c for c in str(prm) if c.isalnum())


def _appel(sens: str, prm: str, jeton: str, debut: date, fin: date) -> pd.Series:
    url = f"{BASE}/{sens}_load_curve/{prm}/start/{debut}/end/{fin}/cache"
    for essai in range(2):  # un seul nouvel essai, sur erreur serveur ; jamais sur 429
        r = _get(url, jeton)
        if r.status_code in (500, 502, 503, 504) and essai == 0:
            time.sleep(5)
            continue
        break
    _DIAG.append(f"{debut}→{fin} : HTTP {r.status_code} {r.text[:160]!r}")
    if r.status_code == 404 and "consentement" in r.text.lower():
        raise RuntimeError("MyElectricalData : pas de consentement pour ce PRM. Faire (ou refaire) le consentement "
                           "Enedis sur myelectricaldata.fr pour ce PRM, puis vérifier que le jeton saisi est bien celui de ce PRM")
    if r.status_code == 404:  # Enedis ne détient rien sur la période
        return pd.Series(dtype=float)
    if r.status_code == 429:
        raise RuntimeError(f"quota de requêtes MyElectricalData atteint ({r.text[:160]!r}), "
                           "nouvel essai à la prochaine tentative autorisée")
    if r.status_code in (401, 403):
        raise RuntimeError(f"jeton refusé ({r.status_code}) : consentement expiré ou jeton à renouveler sur myelectricaldata.fr")
    r.raise_for_status()
    lectures = ((r.json() or {}).get("meter_reading") or {}).get("interval_reading") or []
    fins, vals, prec = [], [], None
    for it in lectures:
        t = pd.Timestamp(datetime.strptime(str(it["date"]), "%Y-%m-%d %H:%M:%S"))
        loc = t.tz_localize(TZ, ambiguous=True, nonexistent="shift_forward")  # heure d'été d'abord
        if prec is not None and loc <= prec:  # heure répétée au passage à l'heure d'hiver
            loc = t.tz_localize(TZ, ambiguous=False, nonexistent="shift_forward")
            if loc <= prec:
                continue
        prec = loc
        fins.append(loc.tz_convert("UTC"))
        vals.append(float(it["value"]) / 1000.0)
    s = pd.Series(vals, index=pd.DatetimeIndex(fins))
    return s[~s.index.duplicated()].sort_index()


def _chercher_emplacement(prm: str) -> str | None:
    """Fiche sans emplacement noté : essaie les secrets ENEDIS_JETON_xx remplis sur ce PRM."""
    for i in range(1, 21):
        nom = f"ENEDIS_JETON_{i:02d}"
        jeton = secret(nom)
        if not jeton:
            continue
        r = _get(f"{BASE}/contracts/{prm}/cache", jeton)
        if r.ok:
            return nom
    return None


def _dossier(sens: str, site_id: str) -> Path:
    return (DOSSIER_CONSO if sens == "consumption" else DOSSIER_DEPOT) / site_id


def _lire_existant(d: Path) -> pd.Series:
    parts = []
    for f in sorted(d.glob("enedis_*.csv")):
        df = pd.read_csv(f, sep=";")
        parts.append(pd.Series(df["puissance_kw"].to_numpy(dtype=float),
                               index=pd.to_datetime(df["horodatage"], utc=True)))
    return pd.concat(parts).sort_index() if parts else pd.Series(dtype=float)


def _ecrire(d: Path, s: pd.Series) -> None:
    d.mkdir(parents=True, exist_ok=True)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    loc = s.index.tz_convert(TZ)
    for mois, part in s.groupby(loc.strftime("%Y-%m")):
        df = pd.DataFrame({"horodatage": part.index.map(lambda t: t.isoformat()),  # UTC, fin d'intervalle
                           "puissance_kw": part.round(4).to_numpy()})
        df.to_csv(d / f"enedis_{mois}.csv", sep=";", index=False)


def _autorise(etat: dict, cle: str, premiere_reprise: bool) -> str | None:
    """None si une tentative est permise maintenant, sinon la raison de l'attente."""
    maintenant = pd.Timestamp.now(tz=TZ)
    e = etat.get(cle) or {}
    n = e.get("tentatives", 0) if e.get("jour") == str(maintenant.date()) else 0
    if premiere_reprise:
        return None if n == 0 else "reprise de l'historique déjà tentée aujourd'hui"
    if maintenant.hour < HEURE_PREMIERE:
        return f"première interrogation du jour à partir de {HEURE_PREMIERE} h"
    if n == 0:
        return None
    if n == 1 and maintenant.hour >= HEURE_SECONDE:
        return None
    if n == 1:
        return f"déjà interrogé ce matin, seconde tentative à partir de {HEURE_SECONDE} h"
    return "déjà interrogé deux fois aujourd'hui, nouvel essai demain"


def _noter(etat: dict, cle: str, statut: str) -> None:
    maintenant = pd.Timestamp.now(tz=TZ)
    e = etat.get(cle) or {}
    n = e.get("tentatives", 0) if e.get("jour") == str(maintenant.date()) else 0
    etat[cle] = {"jour": str(maintenant.date()), "tentatives": n + 1,
                 "derniere": maintenant.isoformat(timespec="minutes"), "statut": statut[:200]}


def actualiser(site: dict, sens: str, prm: str, nom: str | None = None, etat: dict | None = None) -> None:
    etat = {} if etat is None else etat
    cle = f"{prm}_{sens}"
    d = _dossier(sens, site["id"])
    existant = _lire_existant(d)
    fin = aujourd_hui_paris()  # exclu : la veille est la dernière journée disponible
    if len(existant) and existant.index.max() >= pd.Timestamp(fin, tz=TZ).tz_convert("UTC"):
        return  # veille déjà complète : pas d'appel, pour ménager le quota de la passerelle
    raison = _autorise(etat, cle, premiere_reprise=not len(existant))
    if raison:
        journal(f"  {site.get('nom')} : courbe Enedis {sens} de la veille en attente ({raison})")
        return
    _noter(etat, cle, "en cours")
    try:
        statut = _actualiser(site, sens, prm, nom, d, existant, fin)
    except Exception as e:
        _noter_statut(etat, cle, f"échec : {e}")
        raise
    _noter_statut(etat, cle, statut)


def _noter_statut(etat: dict, cle: str, statut: str) -> None:
    etat[cle]["statut"] = statut[:200]


def _actualiser(site: dict, sens: str, prm: str, nom: str | None, d: Path, existant: pd.Series, fin: date) -> str:
    if not nom:
        nom = _chercher_emplacement(prm)
        if not nom:
            journal(f"  {site.get('nom')} : aucun jeton MyElectricalData valable pour le PRM {prm} "
                    "(fiche du site ou secrets ENEDIS_JETON_xx), courbe Enedis non récupérée")
            return "aucun jeton"
        journal(f"  {site.get('nom')} : jeton du PRM {prm} trouvé dans {nom} (emplacement non noté dans la fiche)")
    jeton = secret(nom)
    if not jeton:
        journal(f"  {site.get('nom')} : secret {nom} vide ou non déclaré dans le workflow, courbe Enedis non récupérée")
        return "secret vide"
    debut = fin - timedelta(days=JOURS_REPRISE)
    if len(existant):
        derniere = existant.index.max().tz_convert(TZ).date()
        debut = max(debut, derniere - timedelta(days=2))  # Enedis complète parfois avec retard
    morceaux, d0 = [], debut
    while d0 < fin:
        d1 = min(d0 + timedelta(days=7), fin)
        morceaux.append(_appel(sens, prm, jeton, d0, d1))
        d0 = d1
    nouveau = pd.concat(morceaux) if morceaux else pd.Series(dtype=float)
    if nouveau.empty:
        journal(f"  {site.get('nom')} : aucune courbe Enedis {sens} du {debut} au {fin - timedelta(days=1)}")
        _diagnostic(sens, prm, jeton)
        return "rien reçu"
    _ecrire(d, pd.concat([existant[~existant.index.isin(nouveau.index)], nouveau]))
    journal(f"  {site.get('nom')} : courbe Enedis {'consommation' if sens == 'consumption' else 'production'} "
            f"à jour jusqu'au {nouveau.index.max().tz_convert(TZ):%d/%m %H:%M} ({len(nouveau)} pas récupérés)")
    complet = nouveau.index.max() >= pd.Timestamp(fin, tz=TZ).tz_convert("UTC")
    return "veille complète" if complet else f"reçu jusqu'au {nouveau.index.max().tz_convert(TZ):%d/%m %H:%M}"


def actualiser_tous(sites: list[dict]) -> None:
    """PRM et jeton se saisissent d'abord au niveau du site (champ « PRM du point de livraison ») ;
    les champs PRM de la valorisation et des mesures restent possibles pour un second compteur."""
    etat = lire_json(FICHIER_APPELS, {}) or {}
    for s in sites:
        conso = (s.get("valorisation") or {}).get("conso") or {}
        mes = s.get("mesures") or {}
        prm_site, nom_site = str(s.get("prm") or "").strip(), s.get("jeton_secret")
        def jeton(prm: str, nom: str | None) -> str | None:
            return nom or (nom_site if prm == prm_site else None)
        travaux = []
        if str((s.get("valorisation") or {}).get("mode", "")).startswith("autoconso") and conso.get("source") == "enedis":
            prm = str(conso.get("prm") or prm_site).strip()
            if prm:
                travaux.append(("consumption", prm, jeton(prm, conso.get("jeton_secret"))))
        if mes.get("source") == "enedis":
            prm = str(mes.get("prm_production") or prm_site).strip()
            if prm:
                travaux.append(("production", prm, jeton(prm, mes.get("jeton_secret"))))
        for sens, prm, nom in travaux:
            try:
                actualiser(s, sens, str(prm).strip(), nom, etat)
            except Exception as e:  # un PRM en échec ne bloque pas le calcul
                journal(f"  ! {s.get('nom')} : courbe Enedis {sens} : {e}")
    if etat:
        ecrire_json(FICHIER_APPELS, etat)
