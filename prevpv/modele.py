"""Modèle physique : irradiance prévue -> puissance AC au point de livraison.

Chaîne par champ PV :
1. position du soleil au milieu du quart d'heure (les irradiances sont des moyennes sur le quart d'heure) ;
2. masque d'horizon : la composante directe est supprimée quand le soleil est sous le profil importé ;
3. transposition Perez sur le plan des modules (fixe ou tracker 1 axe avec backtracking) ;
4. pertes angulaires sur le direct (IAM ASHRAE) ;
5. température de cellule (Faiman) avec température et vent prévus ;
6. puissance DC = Pc × G/1000 × (1 + γ(Tc − 25)) × (1 − pertes DC) ;
puis somme des champs, rendement onduleur, écrêtage à la puissance AC et à la limitation d'injection.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pvlib

from .sites import num, pc_totale_kwc

# Coefficients Faiman de départ selon la pose (U0 en W/m²K, U1 en W·s/m³K).
# Valeurs indicatives : l'écart résiduel est absorbé par la correction apprise sur les mesures.
FAIMAN = {
    "sol": (25.0, 6.84),
    "surimpose": (20.0, 4.0),
    "integre": (15.0, 2.0),
}
B0_IAM = 0.05


def position_soleil(site: dict, index: pd.DatetimeIndex) -> pd.DataFrame:
    milieu = index - pd.Timedelta(minutes=7, seconds=30)
    sp = pvlib.solarposition.get_solarposition(milieu, float(site["lat"]), float(site["lon"]),
                                               altitude=num(site.get("altitude"), 0.0))
    sp.index = index
    return sp


def hauteur_masque(points: list | None, azimut_pvlib: pd.Series) -> pd.Series:
    """Hauteur du masque (°) pour chaque azimut solaire. Masque stocké en convention PVsyst (Sud = 0)."""
    if not points:
        return pd.Series(0.0, index=azimut_pvlib.index)
    pts = np.array(sorted(points), dtype=float)
    az = pts[:, 0]
    h = pts[:, 1]
    # fermeture circulaire du profil
    az = np.concatenate([az[-1:] - 360, az, az[:1] + 360])
    h = np.concatenate([h[-1:], h, h[:1]])
    az_pvsyst = (azimut_pvlib.to_numpy() % 360) - 180  # pvlib : Nord = 0 ; PVsyst : Sud = 0
    return pd.Series(np.interp(az_pvsyst, az, h), index=azimut_pvlib.index)


def puissance(site: dict, meteo: pd.DataFrame) -> pd.DataFrame:
    """Retourne un DataFrame : p_ac_kw (modèle brut), poa (W/m², moyenne pondérée), t_cell, elevation."""
    idx = meteo.index
    sp = position_soleil(site, idx)
    ghi = meteo["ghi"].clip(lower=0).fillna(0)
    dhi = meteo["dhi"].clip(lower=0).fillna(0).clip(upper=ghi)
    dni = meteo["dni"].clip(lower=0).fillna(0)

    # Masque d'horizon : direct nul sous le profil
    masque = site.get("masque") or {}
    h_masque = hauteur_masque(masque.get("points"), sp["azimuth"])
    ombre = sp["apparent_elevation"] < h_masque
    dni = dni.where(~ombre, 0.0)

    dni_extra = pvlib.irradiance.get_extra_radiation(idx)
    airmass = pvlib.atmosphere.get_relative_airmass(sp["apparent_zenith"])
    albedo = num(site.get("albedo"), 0.2)
    gamma = num((site.get("modules") or {}).get("gamma_pct"), -0.35) / 100.0
    u0, u1 = FAIMAN.get((site.get("modules") or {}).get("pose", "surimpose"), FAIMAN["surimpose"])
    pertes_dc = num(site.get("pertes_dc_pct"), 3.0) / 100.0
    temp_air = meteo["temp_air"].interpolate().bfill().ffill().fillna(15.0)
    vent = meteo["vent"].interpolate().bfill().ffill().fillna(1.0).clip(lower=0.1)

    p_dc = pd.Series(0.0, index=idx)
    poa_pondere = pd.Series(0.0, index=idx)
    t_cell_pondere = pd.Series(0.0, index=idx)
    pc_tot = pc_totale_kwc(site)

    for pan in site["pans"]:
        pc = num(pan.get("pc_kwc"), 0.0)
        az_pvlib = num(pan.get("azimut"), 0.0) + 180.0
        if pan.get("suivi") == "tracker_1axe":
            tr = pvlib.tracking.singleaxis(sp["apparent_zenith"], sp["azimuth"], axis_tilt=0,
                                           axis_azimuth=az_pvlib, max_angle=num(pan.get("rotation_max"), 55.0),
                                           backtrack=True, gcr=0.35)
            tilt = tr["surface_tilt"].fillna(0.0)
            surf_az = tr["surface_azimuth"].fillna(az_pvlib)
        else:
            tilt = num(pan.get("inclinaison"), 0.0)
            surf_az = az_pvlib

        irr = pvlib.irradiance.get_total_irradiance(tilt, surf_az, sp["apparent_zenith"], sp["azimuth"],
                                                    dni, ghi, dhi, dni_extra=dni_extra, airmass=airmass,
                                                    albedo=albedo, model="perez")
        aoi = pvlib.irradiance.aoi(tilt, surf_az, sp["apparent_zenith"], sp["azimuth"])
        iam = pvlib.iam.ashrae(aoi, b=B0_IAM)
        poa_eff = (irr["poa_direct"].fillna(0) * iam + irr["poa_diffuse"].fillna(0)).clip(lower=0)
        poa_eff = poa_eff.where(sp["apparent_elevation"] > -1, 0.0)
        t_cell = pvlib.temperature.faiman(irr["poa_global"].fillna(0), temp_air, vent, u0=u0, u1=u1)
        p_dc += pc * poa_eff / 1000.0 * (1 + gamma * (t_cell - 25.0)) * (1 - pertes_dc)
        poa_pondere += poa_eff * pc / pc_tot
        t_cell_pondere += t_cell * pc / pc_tot

    ond = site.get("onduleur") or {}
    rendement = num(ond.get("rendement_pct"), 97.0) / 100.0
    pac = num(ond.get("pac_kva"), pc_tot)
    p_ac = (p_dc.clip(lower=0) * rendement).clip(upper=pac)
    lim = num(site.get("limitation_kva"))
    if lim:
        p_ac = p_ac.clip(upper=lim)

    return pd.DataFrame({
        "p_ac_kw": p_ac,
        "poa": poa_pondere,
        "t_cell": t_cell_pondere,
        "elevation": sp["apparent_elevation"],
        "masque": ombre & (sp["apparent_elevation"] > 0),
    })
