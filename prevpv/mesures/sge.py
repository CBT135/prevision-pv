"""Courbes de charge Enedis via les services web SGE (accès tiers, compteurs > 36 kVA).

L'accès est commun à tous les sites dont la source des mesures est « enedis_sge ». Il est saisi dans la page
« Base de données des sites » et rangé dans les secrets du dépôt :
  ENEDIS_SGE_CERT + ENEDIS_SGE_KEY (PEM)  ou  ENEDIS_SGE_P12 (base64) + ENEDIS_SGE_P12_MDP,
  ENEDIS_SGE_LOGIN, ENEDIS_SGE_CONTRAT ; environnement (homologation / production) dans config/sites.json.

État : la saisie et le stockage sont en place ; l'interrogation du service de mesures détaillées
(courbe d'injection du PRM) sera branchée à réception de la documentation et des accès Enedis.
En attendant, les mesures déposées en fichier (GridVis, CSV) restent utilisées pour ces sites.
"""
from __future__ import annotations

from datetime import date

import pandas as pd

from ..commun import journal, secret
from . import depot

_signale: set[str] = set()


def acces_configure() -> bool:
    pem = secret("ENEDIS_SGE_CERT") and secret("ENEDIS_SGE_KEY")
    p12 = secret("ENEDIS_SGE_P12")
    return bool((pem or p12) and secret("ENEDIS_SGE_LOGIN") and secret("ENEDIS_SGE_CONTRAT"))


def production(site: dict, debut: date, fin: date) -> pd.Series | None:
    if site["id"] not in _signale:
        _signale.add(site["id"])
        if acces_configure():
            journal(f"  {site.get('nom')} : accès Enedis SGE enregistré ; interrogation pas encore branchée, "
                    "fichiers déposés utilisés")
        else:
            journal(f"  {site.get('nom')} : source Enedis SGE choisie mais accès non configuré "
                    "(certificat, clé, identifiant, contrat) ; fichiers déposés utilisés")
    return depot.production(site, debut, fin)
