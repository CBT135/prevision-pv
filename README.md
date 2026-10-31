# Prévision PV

Prévision J+1 de la production de centrales photovoltaïques au pas de 15 minutes, calée sur les prix day-ahead, avec calcul du chiffre d'affaires journalier et comparaison prévu / réalisé.

Tout tourne sur GitHub : un calcul automatique deux fois par jour (GitHub Actions) et deux pages web (GitHub Pages) :

- `docs/index.html` : **Prévision PV**, le tableau de bord (prévision J+1, prix, CA, prévu / réalisé) ;
- `docs/sites.html` : **Base de données des sites PV**, la saisie des paramètres des centrales.

## Fonctionnement

Chaque jour, à 6 h 40 et 16 h 20 (heure d'hiver ; 7 h 40 et 17 h 20 en été), le second passage utilisant le run AROME de 12 h UTC :

1. **Réalisé** : récupération des mesures des jours précédents (API Enphase ou CSV déposés), comparaison avec la prévision émise la veille, calcul du CA réalisé.
2. **Correction** : mise à jour, pour chaque site, du facteur de correction appris sur les 30 derniers jours (par tranche d'élévation solaire).
3. **Prix** : prix day-ahead France au pas 15 min de J+1 (ENTSO-E), publiés vers 13 h. Le passage du matin calcule la production sans les prix.
4. **Prévision J+1** : météo Open-Meteo (Météo-France AROME / ARPEGE), modèle physique pvlib, correction, valorisation.
5. Publication des résultats dans `docs/data/`, lus par le tableau de bord.

### Modèle

| Étape | Méthode |
|---|---|
| Météo | GHI, DHI, DNI, température 2 m, vent 10 m au pas 15 min (moyennes sur le quart d'heure), nébulosité horaire interpolée. GTI Open-Meteo du premier champ conservée comme contrôle. |
| Masque | Profil PVsyst importé ; la composante directe est supprimée quand le soleil passe sous l'horizon. |
| Transposition | Perez, albédo du site ; tracker 1 axe avec backtracking (GCR 0,35). |
| Optique | Pertes angulaires ASHRAE (b0 = 0,05) sur le direct. |
| Thermique | Faiman ; coefficients de départ selon la pose (sol 25 / 6,84 ; surimposé 20 / 4 ; intégré 15 / 2). |
| Électrique | Pc × G/1000 × (1 + γ(Tc − 25)) × (1 − pertes DC), rendement onduleur, écrêtage à Pac puis à la limitation d'injection. |
| Correction | Σ mesuré / Σ modèle sur 30 jours, par tranche d'élévation (< 15°, 15–35°, > 35°), bornée 0,6–1,3, active à partir de 10 jours valides. |

### Valorisation

| Mode | Calcul |
|---|---|
| Autoconsommation sans vente du surplus | autoconsommé × prix d'achat évité |
| Autoconsommation avec vente du surplus | autoconsommé × prix évité + surplus × tarif de rachat |
| Vente totale | production × tarif |
| Prix spot | Σ production¼h × (prix¼h − décote) ; option arrêt aux prix négatifs ; option complément de rémunération (estimation journalière) |

Pour l'autoconsommation, la consommation prévue est la moyenne des 4 mêmes jours de semaine précédents (courbe déposée dans `data/conso/<id_site>/`), sinon un profil plat tiré de la consommation annuelle.

## Mise en service

### 1. Créer le dépôt et déposer les fichiers

Créer le dépôt `prevision-pv` sur GitHub, puis y déposer le contenu de ce dossier, **y compris le dossier caché `.github/`** (sur Mac : Cmd + Maj + . pour l'afficher dans le Finder).

**Confidentialité** : un dépôt public expose les coordonnées et paramètres des sites (`config/sites.json`) et les résultats. Pour des centrales clients, préférer un dépôt **privé** : le tableau de bord se lit alors via un jeton (section « Accès aux données d'un dépôt privé » en bas de la page), ou via GitHub Pages si votre offre GitHub le permet (le site publié reste public).

### 2. Activer GitHub Pages

Settings → Pages → *Deploy from a branch* → branche `main`, dossier `/docs`.

### 3. Renseigner les secrets

Settings → Secrets and variables → Actions → *New repository secret* :

| Secret | Usage | Obligatoire |
|---|---|---|
| `ENTSOE_TOKEN` | Prix day-ahead. Créer un compte sur transparency.entsoe.eu, puis demander l'accès à l'API par e-mail à transparency@entsoe.eu (objet « Restful API access ») ; le jeton apparaît ensuite dans les paramètres du compte. | Pour le mode spot |
| `ENPHASE_API_KEY`, `ENPHASE_CLIENT_ID`, `ENPHASE_CLIENT_SECRET` | Application créée sur le portail développeur Enphase (plan gratuit « Watt »). | Pour les sites Enphase |
| `ENPHASE_REFRESH_TOKEN` | Rempli automatiquement par le workflow « Initialisation Enphase » (étape 4). | Pour les sites Enphase |
| `SECRETS_PAT` | Jeton GitHub fine-grained limité à ce dépôt, permission **Secrets : Read and write**. Sert à enregistrer et renouveler le jeton Enphase. | Pour les sites Enphase |
| `OPEN_METEO_API_KEY` | Clé d'abonnement Open-Meteo. L'API gratuite est réservée à l'usage non commercial : nécessaire pour des centrales clients. | Usage commercial |

### 4. Autoriser l'accès Enphase (une seule fois)

1. Ouvrir dans le navigateur, en remplaçant `CLIENT_ID` :
   `https://api.enphaseenergy.com/oauth/authorize?response_type=code&client_id=CLIENT_ID&redirect_uri=https://api.enphaseenergy.com/oauth/redirect_uri`
2. Se connecter avec le compte Enlighten et autoriser l'application : Enphase affiche un **code**.
3. Dans l'onglet **Actions** → *Initialisation Enphase* → *Run workflow*, coller ce code (il expire en quelques minutes).

### 5. Compléter les sites

Ouvrir `https://<compte>.github.io/prevision-pv/sites.html`, saisir le jeton GitHub (fine-grained, ce dépôt, **Contents : Read and write**) dans « Dépôt GitHub », compléter chaque site puis **Enregistrer sur GitHub**. Un site incomplet est ignoré par le calcul.

### 6. Premier lancement

1. Actions → *Reprise d'historique* → *Run workflow* (90 jours par défaut) : la correction est active dès le premier jour.
2. Actions → *Calcul quotidien* → *Run workflow*.
3. Ouvrir `https://<compte>.github.io/prevision-pv/`.

## Mesures déposées en CSV

Pour un site sans API, déposer les fichiers dans `data/mesures_depot/<id_site>/` (production) ou `data/conso/<id_site>/` (consommation). L'identifiant du site figure dans `config/sites.json`.

Formats lus (séparateur `;` ou `,`, virgule décimale acceptée, pas de 15, 30 ou 60 min) :

- `horodatage;puissance_kw` : fin d'intervalle, heure de Paris, puissance moyenne en kW ;
- export de courbe de charge Enedis `Horodate;Valeur` : fin d'intervalle, valeur en W.

## Conventions

- Horodatage : **fin** du quart d'heure, UTC dans les fichiers internes, heure de Paris à l'affichage. Une journée compte 96 pas (92 ou 100 aux changements d'heure).
- Azimuts : convention PVsyst / Open-Meteo, Sud = 0°, Est = −90°, Ouest = +90°. Le masque est stocké dans cette convention.
- Unités : kW, kWh, €/MWh (tarifs résidentiels en €/kWh).

## Limites connues

- Au-delà de ~6 h d'échéance, le pas 15 min des prévisions est interpolé depuis le pas horaire : la forme intra-horaire des journées nuageuses n'est pas prévue.
- La reprise d'historique utilise les prévisions archivées d'Open-Meteo, assemblées à courte échéance : elles sont un peu meilleures qu'une vraie prévision J+1.
- Le complément de rémunération est une estimation journalière ; le calcul contractuel est mensuel, avec son propre prix de marché de référence.
- Les micro-onduleurs sont écrêtés globalement (Pac totale) et non module par module.
- Le masque ne réduit que le direct ; la perte de diffus due à l'horizon est absorbée par la correction.

## Commandes locales (facultatif)

```bash
pip install -r requirements.txt
python -m prevpv.run quotidien
python -m prevpv.run prevision --date 2026-11-02
python -m prevpv.run historique --jours 90
```

## Arborescence

```
config/sites.json             base de données des sites (écrite par docs/sites.html)
prevpv/                       moteur de calcul
  meteo.py  modele.py  correction.py  prix.py  valorisation.py  run.py
  mesures/enphase.py  mesures/depot.py
data/historique/              modèle et mesures par site (apprentissage de la correction)
data/correction.json          facteurs de correction en vigueur
data/mesures_depot/  data/conso/   CSV déposés
docs/index.html  docs/sites.html   pages web
docs/data/                    résultats publiés (prévisions, réalisé, index)
.github/workflows/            calcul quotidien, reprise d'historique, initialisation Enphase
```
