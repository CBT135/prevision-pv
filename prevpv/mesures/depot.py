"""Mesures déposées en fichiers CSV dans le dépôt.

Production :   data/mesures_depot/<id_site>/*.csv
Consommation : data/conso/<id_site>/*.csv   (pour les modes autoconsommation)

Formats acceptés (CSV : séparateur ; ou , — virgule décimale acceptée) :
1. Format du projet : colonnes « horodatage » (fin d'intervalle, heure de Paris ou ISO avec fuseau)
   et « puissance_kw » (puissance moyenne sur l'intervalle).
2. Export de courbe de charge Enedis : colonnes « Horodate » (fin d'intervalle) et « Valeur » en W.
3. Export CSV de GridVis (analyseur Janitza) : ligne « Period: jj/mm/aaaa … », colonne « Date » (h:mm,
   début de l'intervalle, heure de Paris) et colonne de puissance totale « Power (…) [W] », ou à défaut
   somme de toutes les colonnes en W (une par onduleur) ; pas de 5 ou 15 min.
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


def _lire_gridvis(brut: str, chemin: Path) -> pd.Series:
    import csv
    import re
    lignes = [l for l in brut.splitlines() if l.strip()]
    m = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", lignes[0])
    jour0 = pd.to_datetime(m.group(1), dayfirst=True)
    sep = "\t" if "\t" in lignes[1] else (";" if ";" in lignes[1] else ",")
    lect = list(csv.reader(lignes[1:], delimiter=sep))
    entete = lect[0]
    total = [i for i, c in enumerate(entete) if c.lower().startswith("power (") and "[w]" in c.lower()]
    # Puissance totale du point de mesure si elle existe, sinon somme des colonnes en W (onduleurs).
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
        vals.append(_somme_w(r, cols) / 1000.0)
    debut = pd.Series(pd.DatetimeIndex(debuts))
    pas = debut.diff().median()
    s = pd.Series(vals, index=_localiser(debut + pas))
    s = s[s.index.notna()].sort_index()
    if s.isna().any():
        journal(f"  {chemin.name} : {int(s.isna().sum())} valeurs absentes (« x »), non comptées")
    journal(f"  {chemin.name} : export GridVis, {len(s)} valeurs au pas de {int(pas.total_seconds() // 60)} min"
            + (f", somme de {len(cols)} colonnes" if len(cols) > 1 else ""))
    return _au_quart_d_heure(s)


def _lire(chemin: Path) -> pd.Series:
    if chemin.suffix.lower() == ".xlsx":
        return _lire_xlsx(chemin)
    octets = chemin.read_bytes()
    tete = octets.decode("utf-16") if octets[:2] in (b"\xff\xfe", b"\xfe\xff") else octets.decode("utf-8-sig", errors="replace")
    if tete.lstrip('"').startswith("Period:"):
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
    s = _dossier(DOSSIER_DEPOT, site["id"])
    if s is None:
        return None
    a = pd.Timestamp(debut, tz=TZ).tz_convert("UTC")
    b = (pd.Timestamp(fin, tz=TZ) + pd.Timedelta(days=1)).tz_convert("UTC")
    return s[(s.index > a) & (s.index <= b)]


def consommation(site: dict) -> pd.Series | None:
    return _dossier(DOSSIER_CONSO, site["id"])
