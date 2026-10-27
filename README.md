# audit-seo-maroc

Outil d'audit SEO technique automatisé en Python : exploration d'un site (robots.txt, sitemap, title, meta,
H1, images sans alt, liens cassés, HTTPS), mesure Google PageSpeed Insights (LCP, INP, CLS), recommandations
priorisées, **rapport PDF généré automatiquement** et **comparaison de plusieurs sites marocains**.

Projet du module *Webmarketing et CRM* (TDIA3, ENSAH).

## Organisation du code

| Fichier | Rôle |
|---|---|
| `src/audit.py` | Exploration du site et audit technique → `results/<domaine>.json` |
| `src/performance.py` | Mesure PageSpeed Insights (mobile + desktop) → `results/<domaine>_performance.json` |
| `src/recommendations.py` | Fusion audit + performance en recommandations priorisées et score de santé |
| `src/report.py` | Lecture des résultats, liste des problèmes URL par URL, **rapport PDF** |
| `app.py` | **Interface web** (Streamlit) : audit, constats, performance, recommandations, comparaison, PDF |
| `demo_data.py` | Crée 3 sites fictifs pour tester l'interface sans lancer d'audit |
| `tests/` | Tests automatiques (faux sites locaux, aucun accès Internet nécessaire) |

## Installation (Windows, PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # puis ouvrir .env et coller votre clé PSI_API_KEY
```

La clé PageSpeed est gratuite ([obtenir une clé](https://developers.google.com/speed/docs/insights/v5/get-started)).
Sans clé, l'API fonctionne mais avec un quota très limité. Le fichier `.env` ne doit **jamais** être publié sur GitHub
(il est déjà dans `.gitignore`).

## Utilisation

### Interface web (recommandé)

```powershell
streamlit run app.py
```

1. **Tableau de bord** : saisir l'adresse d'un site, cliquer sur « Lancer l'audit » (exploration + PageSpeed + recommandations).
2. Consulter les constats, la performance et les recommandations, puis « Générer le rapport PDF » (rapport du site).
3. Auditer d'autres sites, puis « Comparer plusieurs sites » : choisir les sites et générer le **rapport comparatif**.

Les fichiers sont enregistrés dans `results/` (données) et `reports/` (PDF).

### Ligne de commande

```powershell
python -m src.audit https://www.exemple.ma --max-pages 30
python -m src.performance https://www.exemple.ma
python -m src.report results reports/rapport.pdf        # rapport de tous les sites présents dans results/
```

### Tester l'interface sans attendre un audit

```powershell
python demo_data.py            # crée 3 sites fictifs (demo-*.ma) dans results/
streamlit run app.py
python demo_data.py --clean    # supprime les données de démonstration
```

## Tests

```powershell
python -m pytest -q
```

Les tests démarrent de faux sites locaux et vérifient l'audit, la lecture des résultats, la génération des PDF
(un site, plusieurs sites, données manquantes, site injoignable) et toutes les pages de l'interface.

## Personnaliser le rapport

Dans `src/report.py` : `THEME` (couleurs : `"anthracite"`, `"bordeaux"`, `"vert"`, `"teal"`), `AUTHORS`
(noms affichés sur la couverture) et `COURSE` (ligne du module).

## Limites

- L'exploration porte sur un échantillon de pages (30 par défaut) : les chiffres sont indicatifs pour l'ensemble du site.
- PageSpeed ne teste que l'URL fournie (en général la page d'accueil) ; les mesures varient d'un test à l'autre.
- L'INP n'existe qu'en données terrain : il est « N/D » pour les sites à faible trafic (le TBT en est l'équivalent de laboratoire).
- Le score de santé est un indicateur interne au projet, pas un score Google.
- L'audit ne couvre ni la qualité du contenu, ni les backlinks, ni le SEO local.
