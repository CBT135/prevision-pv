"""Modèle physique : irradiance prévue -> puissance AC au point de livraison.

Chaîne par champ PV :
1. position du soleil au milieu du quart d'heure (les irradiances sont des moyennes sur le quart d'heure) ;
2. masque d'horizon : la composante directe est supprimée quand le soleil est sous le profil importé ;
3. transposition Perez sur le plan des modules (fixe ou tracker 1 axe avec backtracking) ;
4. rangées (centrales au sol, si l'entraxe et la largeur des tables sont saisis) :
   - ombrage mutuel du direct et du circumsolaire, avec effet électrique façon PVsyst : une chaîne
     touchée par l'ombre perd tout son direct (« chaînes en largeur ») ;
   - perte de diffus du ciel masqué par la rangée de devant (angle de masquage de Passias) ;
   - trackers : GCR = largeur / entraxe pour le backtracking ;
5. pertes angulaires sur le direct (IAM ASHRAE) ;
6. température de cellule : modèle PVsyst (Uc, Uv) s'ils sont saisis, sinon Faiman selon la pose ;
7. puissance DC = Pc × G/1000 × (1 + γ(Tc − 25)) × (1 − pertes DC) ;
puis somme des champs, rendement onduleur, écrêtage à la puissance AC et à la limitation d'injection,
pertes AC jusqu'au point de mesure (câbles, transformateur, ligne HTA).
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


def indice_ciel(ghi: pd.Series, sp: pd.DataFrame) -> pd.Series:
    """Indice de ciel clair prévu : GHI prévu / GHI ciel clair (Haurwitz). NaN soleil bas (< 5°)."""
    cs = pvlib.clearsky.haurwitz(sp["apparent_zenith"])["ghi"]
    kt = (ghi / cs).clip(lower=0, upper=1.5)
    return kt.where(sp["apparent_elevation"] >= 5)


def fraction_ombree(elevation: pd.Series, azimut_soleil: pd.Series, tilt: float, azimut_surface: float,
                    pitch: float, largeur: float, bande_inactive: float = 0.0) -> pd.Series:
    """Fraction de la largeur de table à l'ombre de la rangée de devant (rangées infinies, terrain plat).

    Angle de profil αp : tan αp = tan(h) / cos(γs − γ). Longueur éclairée = P·sin αp / sin(αp + β).
    Azimuts en convention pvlib (Nord = 0)."""
    h = np.radians(elevation.clip(lower=0).to_numpy())
    dgam = np.radians(azimut_soleil.to_numpy() - azimut_surface)
    cosd = np.cos(dgam)
    beta = np.radians(tilt)
    with np.errstate(divide="ignore", invalid="ignore"):
        ap = np.arctan2(np.tan(h), cosd)  # 0..π ; > π/2 : soleil derrière les modules
        eclaire = pitch * np.sin(ap) / np.sin(ap + beta)
        f = 1.0 - (eclaire - bande_inactive) / (largeur - 2 * bande_inactive)
    f = np.where((ap <= 0) | (ap + beta >= np.pi) | (cosd <= 0), 0.0, f)
    f = np.where(elevation.to_numpy() <= 0, 0.0, f)
    return pd.Series(np.clip(np.nan_to_num(f, nan=0.0), 0.0, 1.0), index=elevation.index)


def perte_electrique(f: pd.Series, chaines_largeur: int) -> pd.Series:
    """Fraction du direct perdue : toute chaîne touchée par l'ombre perd son direct (modèle PVsyst)."""
    n = max(int(chaines_largeur or 1), 1)
    return pd.Series(np.minimum(1.0, np.ceil(f.to_numpy() * n - 1e-9) / n), index=f.index).clip(lower=f)


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
    mod = site.get("modules") or {}
    u0, u1 = FAIMAN.get(mod.get("pose", "surimpose"), FAIMAN["surimpose"])
    uc, uv = num(mod.get("uc")), num(mod.get("uv"), 0.0)
    eff_mod = num(mod.get("rendement_pct"), 15.0) / 100.0
    pertes_dc = num(site.get("pertes_dc_pct"), 3.0) / 100.0
    temp_air = meteo["temp_air"].interpolate().bfill().ffill().fillna(15.0)
    vent = meteo["vent"].interpolate().bfill().ffill().fillna(1.0).clip(lower=0.1)

    p_dc = pd.Series(0.0, index=idx)
    poa_pondere = pd.Series(0.0, index=idx)
    t_cell_pondere = pd.Series(0.0, index=idx)
    ombre_rangees = pd.Series(0.0, index=idx)
    pc_tot = pc_totale_kwc(site)

    for pan in site["pans"]:
        pc = num(pan.get("pc_kwc"), 0.0)
        az_pvlib = num(pan.get("azimut"), 0.0) + 180.0
        pitch, larg = num(pan.get("pitch_m")), num(pan.get("largeur_m"))
        rangees = bool(pitch and larg and pitch > larg > 0)
        gcr = larg / pitch if rangees else 0.35
        if pan.get("suivi") == "tracker_1axe":
            tr = pvlib.tracking.singleaxis(sp["apparent_zenith"], sp["azimuth"], axis_tilt=0,
                                           axis_azimuth=az_pvlib, max_angle=num(pan.get("rotation_max"), 55.0),
                                           backtrack=True, gcr=gcr)
            tilt = tr["surface_tilt"].fillna(0.0)
            surf_az = tr["surface_azimuth"].fillna(az_pvlib)
        else:
            tilt = num(pan.get("inclinaison"), 0.0)
            surf_az = az_pvlib

        aoi = pvlib.irradiance.aoi(tilt, surf_az, sp["apparent_zenith"], sp["azimuth"])
        direct = pvlib.irradiance.beam_component(tilt, surf_az, sp["apparent_zenith"], sp["azimuth"], dni).fillna(0)
        diff = pvlib.irradiance.perez(tilt, surf_az, dhi, dni, dni_extra, sp["apparent_zenith"], sp["azimuth"],
                                      airmass, return_components=True)
        iso = diff["poa_isotropic"].fillna(0)
        circ = diff["poa_circumsolar"].fillna(0)
        horiz = diff["poa_horizon"].fillna(0)
        sol = pvlib.irradiance.get_ground_diffuse(tilt, ghi, albedo=albedo).fillna(0)
        if rangees:
            # ombrage du direct et du circumsolaire par la rangée de devant (pas d'ombrage avec backtracking)
            if pan.get("suivi") != "tracker_1axe":
                f = fraction_ombree(sp["apparent_elevation"], sp["azimuth"], float(tilt), float(surf_az),
                                    pitch, larg, num(pan.get("bande_inactive_m"), 0.0))
                fe = perte_electrique(f, int(num(pan.get("chaines_largeur"), 1)))
                direct = direct * (1 - fe)
                circ = circ * (1 - fe)
                ombre_rangees += fe * pc / pc_tot
            # diffus du ciel masqué par la rangée de devant (isotrope + bande d'horizon)
            masq = pvlib.shading.masking_angle_passias(tilt, gcr)
            perte = pd.Series(np.asarray(pvlib.shading.sky_diffuse_passias(masq), dtype=float), index=idx)
            iso = iso * (1 - perte)
            horiz = horiz * (1 - perte)
            sol = sol * (1 - gcr)  # le sol vu entre les rangées est en partie à l'ombre
        iam = pvlib.iam.ashrae(aoi, b=B0_IAM)
        poa_glob = (direct + circ + iso + horiz + sol).clip(lower=0)
        poa_eff = ((direct + circ) * iam + iso + horiz + sol).clip(lower=0)
        poa_eff = poa_eff.where(sp["apparent_elevation"] > -1, 0.0)
        if uc:
            t_cell = pvlib.temperature.pvsyst_cell(poa_glob, temp_air, vent, u_c=uc, u_v=uv,
                                                   module_efficiency=eff_mod, alpha_absorption=0.9)
        else:
            t_cell = pvlib.temperature.faiman(poa_glob, temp_air, vent, u0=u0, u1=u1)
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
    p_ac = p_ac * (1 - num(site.get("pertes_ac_pct"), 0.0) / 100.0)

    return pd.DataFrame({
        "p_ac_kw": p_ac,
        "poa": poa_pondere,
        "t_cell": t_cell_pondere,
        "elevation": sp["apparent_elevation"],
        "kt": indice_ciel(ghi, sp),
        "masque": ombre & (sp["apparent_elevation"] > 0),
        "ombre_rangees": ombre_rangees,
    })
