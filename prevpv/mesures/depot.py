"""Mesures déposées en fichiers CSV dans le dépôt.

Production :   data/mesures_depot/<id_site>/*.csv
Consommation : data/conso/<id_site>/*.csv   (pour les modes autoconsommation)

Formats acceptés (CSV : séparateur ; ou , — virgule décimale acceptée) :
1. Format du projet : colonnes « horodatage » (fin d'intervalle, heure de Paris ou ISO avec fuseau)
   et « puissance_kw » (puissance moyenne sur l'intervalle).
2. Export de courbe de charge Enedis : colonnes « Horodate » (fin d'intervalle) et « Valeur » en W.
3. Export CSV de GridVis (analyseur Janitza), en anglais ou en français : ligne « Period: » ou « Période: jj/mm/aaaa … », colonne « Date » (h:mm,
   début de l'intervalle, heure de Paris) et colonne de puissance totale « Power (…) [W] », ou à défaut
   somme de toutes les colonnes en W (une par onduleur) ; pas de 5 ou 15 min.
   Avec une colonne par onduleur, les quarts d'heure où un onduleur est en défaut (moins de la moitié
   de son voisin de poste, ou poste entier à l'arrêt) sont repérés et exclus de l'apprentissage de la
   correction (defauts()).
   Puissance positive = injection.
4. Export Excel de l'espace client Enedis (.xlsx, onglet « Puissance moyenne soutirée » ou
   « injectée ») : colonnes « Début », « Fin », « Valeur (en kW) », heure de Paris.
Les pas de 30 ou 60 min sont ramenés au quart d'heure (puissance constante sur l'intervalle).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from ..commun import DOSSIER_CONSO, DOSSIER_DEPOT, TZ, journal


def _localiser(temps: pd.Series) -> pd.DatetimeIndex:
    if temps.dt.tz is None:
        try:
            temps = temps.dt.tz_localize(TZ, ambiguous="infer", nonexistent="NaT")
        except Exception:
            temps = temps.dt.tz_localize(TZ, ambiguous="NaT", nonexistent="NaT")
    return pd.DatetimeIndex(temps.dt.tz_convert("UTC"))


def _lire_xlsx(chemin: Path) -> pd.Series:
    import openpyxl
    wb = openpyxl.load_workbook(chemin, read_only=True, data_only=True)
    fins, vals = [], []
    for ws in wb.worksheets:
        col = None
        for r in ws.iter_rows(values_only=True):
            cellules = [str(c).strip().lower() if c is not None else "" for c in r]
            if col is None:
                if "fin" in cellules and any(c.startswith("valeur") for c in cellules):
                    col = (cellules.index("fin"), next(i for i, c in enumerate(cellules) if c.startswith("valeur")))
                continue
            f, v = r[col[0]], r[col[1]]
            if f is None or v is None:
                continue
            fins.append(f)
            vals.append(v)
    if not fins:
        journal(f"  ! {chemin.name} : aucune colonne « Fin » / « Valeur » trouvée")
        return pd.Series(dtype=float)
    temps = pd.Series(pd.to_datetime(pd.Series(fins).astype(str), dayfirst=True, errors="coerce", format="mixed"))
    val = pd.to_numeric(pd.Series(vals).astype(str).str.replace(",", ".", regex=False), errors="coerce")
    s = pd.Series(val.to_numpy(), index=_localiser(temps)).dropna()
    s = s[s.index.notna()].sort_index()
    return _au_quart_d_heure(s)


def _somme_w(r: list, cols: list) -> float:
    """Somme des colonnes en W ; une valeur absente (« x », vide) rend le total inconnu (NaN)."""
    total = 0.0
    for c in cols:
        v = r[c].strip().replace(",", ".")
        try:
            total += float(v)
        except ValueError:
            return float("nan")
    return total


def _tableau_gridvis(brut: str) -> tuple[pd.DatetimeIndex, list[str], list[list[float]], bool]:
    """Export GridVis → (fins d'intervalle UTC, noms de colonnes, valeurs en W par ligne, colonne totale ?)."""
    import csv
    import re
    lignes = [l for l in brut.splitlines() if l.strip()]
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", lignes[0])
    jour0 = pd.to_datetime(m.group(1), dayfirst=True)
    sep = "\t" if "\t" in lignes[1] else (";" if ";" in lignes[1] else ",")
    lect = list(csv.reader(lignes[1:], delimiter=sep))
    entete = lect[0]
    # colonne de puissance totale du point de mesure : « Power (…) » (export anglais) ou « Puissance (…) »
    # (export français) ; pas les colonnes par phase ni « Puissance de courant alternatif (onduleur) »
    total = [i for i, c in enumerate(entete)
             if c.lower().startswith(("power (", "puissance (")) and "[w]" in c.lower()]
    # Puissance totale du point de mesure si elle existe, sinon une colonne en W par onduleur.
    cols = total[:1] or [i for i, c in enumerate(entete) if i > 0 and "[w]" in c.lower()]
    debuts, vals, jour, prec = [], [], jour0, None
    for r in lect[1:]:
        if len(r) <= max(cols) or not r[0].strip():
            continue
        t = r[0].strip()
        h = pd.to_timedelta(t + ":00") if t.count(":") == 1 else pd.to_timedelta(t)
        if prec is not None and h < prec:  # passage à minuit dans un export de plusieurs jours
            jour = jour + pd.Timedelta(days=1)
        prec = h
        debuts.append(jour + h)
        ligne = []
        for c in cols:
            try:
                ligne.append(float(r[c].strip().replace(",", ".")))
            except ValueError:
                ligne.append(float("nan"))
        vals.append(ligne)
    debut = pd.Series(pd.DatetimeIndex(debuts))
    pas = debut.diff().median()
    return _localiser(debut + pas), [entete[c] for c in cols], vals, bool(total)


def _lire_gridvis(brut: str, chemin: Path) -> pd.Series:
    idx, noms, vals, _ = _tableau_gridvis(brut)
    # une valeur absente (« x », vide) rend le total inconnu (NaN)
    s = pd.Series([sum(l) / 1000.0 for l in vals], index=idx)
    s = s[s.index.notna()].sort_index()
    if s.isna().any():
        journal(f"  {chemin.name} : {int(s.isna().sum())} valeurs absentes (« x »), non comptées")
    pas = s.index.to_series().diff().median()
    journal(f"  {chemin.name} : export GridVis, {len(s)} valeurs au pas de {int(pas.total_seconds() // 60)} min"
            + (f", somme de {len(noms)} colonnes" if len(noms) > 1 else ""))
    return _au_quart_d_heure(s)


# Défaut d'onduleur (déclenchement sur défaut d'isolement par pluie ou humidité matinale, etc.).
# Les onduleurs d'un même poste (« 1.1 » et « 1.2 ») voient le même ciel et les mêmes ombrages :
# un onduleur est en défaut s'il produit moins de la moitié de son voisin de poste, ou moins de 15 %
# du meilleur des autres onduleurs (poste entier à l'arrêt), quand la référence dépasse 5 % de la plus
# forte puissance d'onduleur de la journée.
SEUIL_VOISIN = 0.5
SEUIL_GLOBAL = 0.15
SEUIL_ACTIVITE = 0.05


def _poste(nom: str) -> str:
    import re
    m = re.search(r"(\d+)[.\-_](\d+)\D*$", nom.split("[")[0].strip(' ")'))
    return m.group(1) if m else nom


def _defauts_gridvis(brut: str) -> pd.Series:
    """Quarts d'heure où au moins un onduleur est en défaut (True), export GridVis par onduleur."""
    import numpy as np
    idx, noms, vals, total = _tableau_gridvis(brut)
    if total or len(noms) < 2:
        return pd.Series(dtype=bool)
    a = np.nan_to_num(np.array(vals, dtype=float), nan=0.0)
    pmax = a.max() if a.size else 0.0
    actif = SEUIL_ACTIVITE * pmax
    postes = [_poste(n) for n in noms]
    defaut = np.zeros(len(a), dtype=bool)
    for j in range(a.shape[1]):
        autres = [k for k in range(a.shape[1]) if k != j]
        voisins = [k for k in autres if postes[k] == postes[j]]
        if voisins:
            ref = a[:, voisins].max(axis=1)
            defaut |= (ref > actif) & (a[:, j] < SEUIL_VOISIN * ref)
        ref = a[:, autres].max(axis=1)
        defaut |= (ref > actif) & (a[:, j] < SEUIL_GLOBAL * ref)
    s = pd.Series(defaut, index=idx)
    s = s[s.index.notna()].sort_index()
    s = s[~s.index.duplicated()]
    pas = s.index.to_series().diff().median()
    if pd.notna(pas) and pas < pd.Timedelta(minutes=15):  # pas de 5 min : quart d'heure en défaut si l'un l'est
        s = s.resample("15min", closed="right", label="right").max().astype(bool)
    return s


def _texte(chemin: Path) -> str:
    octets = chemin.read_bytes()
    return octets.decode("utf-16") if octets[:2] in (b"\xff\xfe", b"\xfe\xff") else octets.decode("utf-8-sig", errors="replace")


def _est_gridvis(tete: str) -> bool:
    return tete.lstrip('\ufeff"').startswith(("Period:", "Période:", "Periode:"))


def _lire(chemin: Path) -> pd.Series:
    if chemin.suffix.lower() == ".xlsx":
        return _lire_xlsx(chemin)
    tete = _texte(chemin)
    if _est_gridvis(tete):
        return _lire_gridvis(tete, chemin)
    brut = chemin.read_text(encoding="utf-8-sig", errors="replace")
    sep = ";" if brut.count(";") >= brut.count(",") else ","
    df = pd.read_csv(chemin, sep=sep, dtype=str, encoding="utf-8-sig", encoding_errors="replace")
    cols = {c.lower().strip(): c for c in df.columns}
    if "horodatage" in cols and "puissance_kw" in cols:
        t, v, facteur = cols["horodatage"], cols["puissance_kw"], 1.0
    elif "horodate" in cols and "valeur" in cols:
        t, v, facteur = cols["horodate"], cols["valeur"], 0.001
    else:
        journal(f"  ! {chemin.name} : colonnes non reconnues {list(df.columns)}")
        return pd.Series(dtype=float)
    temps = pd.to_datetime(df[t], utc=False, errors="coerce", format="mixed")
    val = pd.to_numeric(df[v].str.replace(",", ".", regex=False).str.strip(), errors="coerce") * facteur
    s = pd.Series(val.to_numpy(), index=_localiser(temps)).dropna()
    s = s[s.index.notna()].sort_index()
    return _au_quart_d_heure(s)


def _au_quart_d_heure(s: pd.Series) -> pd.Series:
    s = s[~s.index.duplicated()]
    if len(s) < 2:
        return s
    pas = s.index.to_series().diff().median()
    if pas < pd.Timedelta(minutes=15):  # pas fin (5 min) : moyenne par quart d'heure
        return s.resample("15min", closed="right", label="right").mean().dropna()
    if pas == pd.Timedelta(minutes=15):
        return s
    n = int(round(pas / pd.Timedelta(minutes=15)))
    morceaux = [pd.Series(s.to_numpy(), index=s.index - pd.Timedelta(minutes=15 * k)) for k in range(n)]
    return pd.concat(morceaux).sort_index()


def _dossier(base: Path, site_id: str) -> pd.Series | None:
    d = base / site_id
    fichiers = sorted([*d.glob("*.csv"), *d.glob("*.xlsx")]) if d.exists() else []
    if not fichiers:
        return None
    s = pd.concat([_lire(f) for f in fichiers])
    return s[~s.index.duplicated(keep="last")].sort_index()


def production(site: dict, debut: date, fin: date) -> pd.Series | None:
    # « copie_de » : site jumeau (même centrale, autre valorisation) qui lit les fichiers déposés de l'original
    s = _dossier(DOSSIER_DEPOT, (site.get("mesures") or {}).get("copie_de") or site["id"])
    if s is None:
        return None
    a = pd.Timestamp(debut, tz=TZ).tz_convert("UTC")
    b = (pd.Timestamp(fin, tz=TZ) + pd.Timedelta(days=1)).tz_convert("UTC")
    return s[(s.index > a) & (s.index <= b)]


def defauts(site: dict) -> pd.Series:
    """Quarts d'heure (fin, UTC) où un onduleur était en défaut, d'après les exports GridVis par onduleur."""
    d = DOSSIER_DEPOT / ((site.get("mesures") or {}).get("copie_de") or site["id"])
    morceaux = []
    for f in sorted(d.glob("*.csv")) if d.exists() else []:
        try:
            t = _texte(f)
            if _est_gridvis(t):
                morceaux.append(_defauts_gridvis(t))
        except Exception as e:
            journal(f"  ! {f.name} : détection des défauts onduleur impossible ({e})")
    if not morceaux:
        return pd.Series(dtype=bool)
    s = pd.concat(morceaux)
    return s[~s.index.duplicated(keep="last")].sort_index()


def consommation(site: dict) -> pd.Series | None:
    return _dossier(DOSSIER_CONSO, site["id"])
