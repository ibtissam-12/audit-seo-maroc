# Partie 3 — Interface et rapport PDF (SEO Audit Maroc)

## Fichiers
- `app.py` : interface Streamlit, navigation, affichage des résultats et export PDF.
- `src/report.py` : génération du rapport A4 avec ReportLab.
- `requirements.txt` : dépendances de cette partie.

## Installation (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

## Intégration avec les autres membres
Copiez `app.py` et `src/report.py` dans le dépôt commun. Ajoutez `src/__init__.py`.
L'application attend les fonctions suivantes :
- `src/audit.py` : `audit_site(url)` -> dictionnaire de résultats techniques.
- `src/performance.py` : `analyze_performance(url)` -> dictionnaire contenant LCP/INP/CLS si disponibles.
- `src/recommendations.py` : `build_issues(results)` -> liste de problèmes/recommandations.

La fonction `generate_pdf(results, path)` attend un dictionnaire de la forme :
```python
{
  "url": "https://example.ma",
  "generated_at": "2026-10-24 12:30",
  "technical": {"pages_crawled": 8, "https": True},
  "performance": {"LCP": "2.1 s", "INP": "180 ms", "CLS": 0.05},
  "issues": [
    {"issue": "Meta description manquante", "url": "https://example.ma/",
     "severity": "High", "recommendation": "Ajouter une description unique."}
  ]
}
```

Le code ne fabrique pas de valeurs d'audit : si une mesure n'est pas disponible, il affiche « Données indisponibles ».
