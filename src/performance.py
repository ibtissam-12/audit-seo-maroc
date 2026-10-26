"""
MODULE B (Personne 2) : MESURE DE PERFORMANCE (Google PageSpeed Insights)
=========================================================================

CE QUE FAIT CE FICHIER (en bref)
--------------------------------
Il demande à Google de tester la vitesse d'un site (sur mobile, puis sur ordinateur) et retourne
les résultats sous forme d'un dictionnaire (compatible JSON), enregistré aussi dans un fichier.
C'est le même principe que audit.py : UNE fonction principale, UNE sortie que les autres lisent.

Ce qu'il récupère (via l'API Google PageSpeed Insights, abrégée PSI) :
    - le score de performance Lighthouse, de 0 à 100, avec un verdict (bon / à améliorer / mauvais) ;
    - les mesures de laboratoire : FCP, LCP, TBT, CLS, Speed Index, TTI (chacune avec son verdict) ;
    - les données terrain (vrais visiteurs de Chrome, "CrUX") quand Google en possède ;
    - les 5 principales opportunités d'amélioration proposées par Google ;
    - en plus, deux catégories que audit.py ne couvre PAS : l'ACCESSIBILITÉ (contrastes, libellés,
      navigation au clavier...) et les BONNES PRATIQUES (erreurs de console, bibliothèques obsolètes,
      images déformées...), chacune avec un score /100 et ses 5 problèmes les plus importants.

Il ne juge PAS le SEO technique (balises, liens, robots.txt...) : c'est le travail de audit.py.
La catégorie "seo" de Lighthouse n'est volontairement PAS demandée : elle ferait double emploi
avec audit.py (title, meta, alt, canonical, viewport, robots).
Il ne fabrique PAS de recommandations : c'est le travail de recommendations.py.

Place dans l'architecture (projet à 3 personnes)
------------------------------------------------
    Personne 1 :  audit.py          -->  results/<site>.json
    Personne 2 :  performance.py    -->  results/<site>_performance.json      (CE FICHIER)
    Personne 2 :  recommendations.py  lit les deux fichiers ci-dessus et classe les actions par priorité
    Personne 3 :  report.py / app.py  lisent les sorties des trois modules ci-dessus

    Qui lit quoi dans la sortie de performance_site() :
        recommendations.py : api_available, error, mobile.score, mobile.lab (lcp_s, tbt_ms)
        report.py          : summary + mobile / desktop (score, lab, field, google_opportunities)
        app.py             : appelle performance_site(url) directement, puis teste "error"

Ordre d'exécution de performance_site()
---------------------------------------
    1. call_pagespeed    appel de l'API Google (mobile puis desktop), avec nouveaux essais
    2. parse_pagespeed   on ne garde que l'essentiel du très gros JSON renvoyé par Google
    3. summary           chiffres clés pour comparer plusieurs sites d'un coup d'œil

CONTRAT DE SORTIE (NE PAS changer les noms de clés sans prévenir l'équipe)
-------------------------------------------------------------------------
Succès :
    {
      "url": "https://www.exemple.ma", "domain": "exemple.ma", "measured_at": "2026-10-25T14:30:00",
      "api_available": true,
      "mobile":  {"available": true, "score": 63, "rating": "à améliorer", "final_url": "...",
                  "lab": {"fcp_s": 2.1, "lcp_s": 4.8, "tbt_ms": 310, "cls": 0.02, "speed_index_s": 4.4, "tti_s": 6.0},
                  "lab_rating": {"fcp_s": "à améliorer", "lcp_s": "mauvais", ...},
                  "field": {"lcp_s": {"value": 3.9, "category": "AVERAGE"}, ..., "overall": "AVERAGE"}  # ou null
                  "google_opportunities": [{"id", "title", "savings_ms", "display_value", "description"}, ...],
                  "accessibility": {"score": 87, "rating": "à améliorer",
                                    "issues": [{"id", "title", "display_value", "weight"}, ...]},   # max 5
                  "best_practices": {"score": 96, "rating": "bon", "issues": []},
                  "error": null},
      "desktop": { ...même structure... },
      "summary": {"mobile_score": 63, "desktop_score": 88, "mobile_lcp_s": 4.8, "mobile_cls": 0.02,
                  "mobile_tbt_ms": 310, "worst_rating": "à améliorer",
                  "mobile_accessibility": 87, "mobile_best_practices": 96,
                  "desktop_accessibility": 90, "desktop_best_practices": 96}
    }
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

# Le fichier .env (à la racine du projet) contient la clé API : PSI_API_KEY=xxxx
# python-dotenv le lit et le met dans les variables d'environnement. S'il n'est pas installé,
# on continue : la variable peut aussi être définie à la main (export PSI_API_KEY=...).
try:
    from dotenv import load_dotenv
    load_dotenv()                      # cherche .env dans le dossier du fichier, puis dans les dossiers parents
except ImportError:
    pass

# ------------------------------------------------------------------ réglages
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
PSI_TIMEOUT = 90                      # PSI lance un vrai test Chrome : il peut prendre 30-60 s
RETRY_WAITS = (3, 8, 15)              # pauses (en secondes) avant chaque nouvel essai
RETRY_CODES = {429, 500, 502, 503, 504}   # erreurs TEMPORAIRES : cela vaut la peine de réessayer
                                          # (les autres, ex. 400 "page introuvable", sont définitives)

# Seuils officiels Google : (limite du "bon", limite du "à améliorer") ; au-delà = "mauvais"
THRESHOLDS = {
    "fcp_s": (1.8, 3.0),              # First Contentful Paint : 1er contenu affiché (secondes)
    "lcp_s": (2.5, 4.0),              # Largest Contentful Paint : plus gros élément affiché (secondes)
    "tbt_ms": (200, 600),             # Total Blocking Time : page figée pendant le chargement (ms)
    "cls": (0.1, 0.25),               # Cumulative Layout Shift : la page "saute" pendant le chargement
    "speed_index_s": (3.4, 5.8),      # Speed Index : vitesse d'affichage visuel (secondes)
    "tti_s": (3.8, 7.3),              # Time To Interactive : page utilisable (secondes)
}


# =================================================================== UTILITAIRES
def _host(url: str) -> str:
    """RÔLE : extraire le nom de domaine d'une URL, sans le 'www.'.
    Sert à nommer le fichier de sortie, avec la même règle que audit.py
    (https://www.exemple.ma/page -> 'exemple.ma'), pour que tous les fichiers du projet se retrouvent."""
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _rate(metric: str, value):
    """RÔLE : donner un verdict à UNE mesure en la comparant aux seuils officiels de Google.
    ENTRÉE : le nom de la mesure (clé de THRESHOLDS) et sa valeur.
    SORTIE : 'bon', 'à améliorer' ou 'mauvais' ; None si la mesure est absente ou inconnue."""
    if value is None or metric not in THRESHOLDS:
        return None
    good, ok = THRESHOLDS[metric]
    return "bon" if value <= good else "à améliorer" if value <= ok else "mauvais"


def _score_rating(score):
    """RÔLE : donner un verdict au score global Lighthouse (échelle de Google).
    SORTIE : 'bon' (90-100), 'à améliorer' (50-89), 'mauvais' (0-49) ; None si pas de score."""
    if score is None:
        return None
    return "bon" if score >= 90 else "à améliorer" if score >= 50 else "mauvais"


# =================================================================== 1. APPEL À L'API PSI
def _api_message(r: requests.Response) -> str:
    """RÔLE : extraire le message d'erreur lisible renvoyé par Google dans sa réponse.
    Si la réponse n'est pas du JSON attendu, on retourne simplement le code HTTP (ex. 'HTTP 502')."""
    try:
        return r.json()["error"]["message"][:160]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {r.status_code}"


def call_pagespeed(url: str, strategy: str, api_key: str | None):
    """RÔLE : appeler l'API PageSpeed Insights pour UNE stratégie ('mobile' ou 'desktop').
    GESTION DES ERREURS (sans jamais lever d'exception) :
    - erreur TEMPORAIRE (429 quota, 5xx, timeout, coupure réseau) : jusqu'à 3 nouveaux essais,
      avec des pauses de plus en plus longues (RETRY_WAITS) ;
    - erreur DÉFINITIVE (ex. 400 : Google n'arrive pas à ouvrir la page) : abandon immédiat,
      inutile d'insister.
    Sans clé API, le quota gratuit se vide vite : on le dit dans le message d'erreur.
    SORTIE : (json_brut, None) si succès ; (None, message_d_erreur_lisible) si échec."""
    # 3 catégories dans le MÊME appel (pas de quota supplémentaire) : performance, accessibility,
    # best-practices. La catégorie "seo" n'est pas demandée : audit.py s'en charge.
    # locale=fr : descriptions et titres en français
    params = [("url", url), ("strategy", strategy), ("locale", "fr"),
              ("category", "performance"), ("category", "accessibility"), ("category", "best-practices")]
    if api_key:
        params.append(("key", api_key))
    last_error = "erreur inconnue"
    for attempt in range(len(RETRY_WAITS) + 1):          # 1 essai + 3 nouveaux essais
        try:
            r = requests.get(PSI_URL, params=params, timeout=PSI_TIMEOUT)
            if r.status_code == 200:
                return r.json(), None
            last_error = _api_message(r)
            if r.status_code == 429 and not api_key:
                last_error += " (quota dépassé : ajoutez une clé API PSI_API_KEY)"
            if r.status_code not in RETRY_CODES:         # erreur définitive : on n'insiste pas
                return None, f"API PageSpeed : {last_error}"
        except requests.exceptions.Timeout:
            last_error = "délai dépassé (timeout)"
        except (requests.RequestException, ValueError) as e:
            last_error = str(e)[:100]
        if attempt < len(RETRY_WAITS):
            time.sleep(RETRY_WAITS[attempt])             # pause avant de réessayer
    return None, f"API PageSpeed indisponible après {len(RETRY_WAITS) + 1} essais : {last_error}"


# =================================================================== 2. LECTURE DU RÉSULTAT PSI
def parse_pagespeed(data: dict) -> dict:
    """RÔLE : ne garder du JSON de Google (plusieurs centaines de Ko) que l'essentiel.
    EXTRAIT :
    - le score Lighthouse (0-100) et son verdict ;
    - les mesures de LABORATOIRE (test simulé par Google) : FCP, LCP, TBT, CLS, Speed Index, TTI,
      chacune avec son verdict (bon / à améliorer / mauvais) ;
    - les données TERRAIN (CrUX = vrais visiteurs de Chrome sur 28 jours) : LCP, CLS, INP, FCP.
      Elles n'existent que pour les sites assez visités : sinon la clé "field" vaut None ;
    - le top 5 des opportunités de gain de temps proposées par Google (affichées telles quelles
      dans le rapport ; recommendations.py ne les répète PAS).
    - les catégories ACCESSIBILITÉ et BONNES PRATIQUES : score /100 + 5 principaux problèmes
      (absents de audit.py, donc non redondants).
    NOTE : INP (réactivité réelle) n'existe qu'en données terrain ; en laboratoire c'est le TBT qui
    sert d'approximation.
    Lève KeyError/TypeError si la réponse n'a pas la structure attendue (gérée par performance_site).
    SORTIE : dictionnaire (voir le contrat en haut du fichier)."""
    lh = data["lighthouseResult"]
    audits = lh.get("audits", {})

    def num(audit_id):
        """valeur numérique brute d'un audit Lighthouse (ms pour les temps), ou None"""
        return (audits.get(audit_id) or {}).get("numericValue")

    def sec(v):
        """millisecondes -> secondes arrondies à 2 décimales"""
        return round(v / 1000, 2) if v is not None else None

    raw_score = (lh.get("categories", {}).get("performance") or {}).get("score")   # entre 0 et 1
    score = int(round(raw_score * 100)) if raw_score is not None else None
    cls, tbt = num("cumulative-layout-shift"), num("total-blocking-time")
    lab = {
        "fcp_s": sec(num("first-contentful-paint")),
        "lcp_s": sec(num("largest-contentful-paint")),
        "tbt_ms": round(tbt) if tbt is not None else None,
        "cls": round(cls, 3) if cls is not None else None,
        "speed_index_s": sec(num("speed-index")),
        "tti_s": sec(num("interactive")),
    }
    lab_rating = {k: _rate(k, v) for k, v in lab.items()}

    # Données terrain : d'abord celles de la page elle-même, sinon celles de tout le domaine ("origin")
    exp = data.get("loadingExperience") or {}
    if not exp.get("metrics"):
        exp = data.get("originLoadingExperience") or {}
    fm = exp.get("metrics") or {}

    def field(key, scale=1.0):
        """une mesure terrain : valeur au 75e percentile (mise à l'échelle) + catégorie Google"""
        m = fm.get(key)
        if not m or m.get("percentile") is None:
            return None
        return {"value": round(m["percentile"] * scale, 3), "category": m.get("category")}

    field_data = {
        "lcp_s": field("LARGEST_CONTENTFUL_PAINT_MS", 0.001),      # ms -> s
        "cls": field("CUMULATIVE_LAYOUT_SHIFT_SCORE", 0.01),       # Google renvoie CLS x 100
        "inp_ms": field("INTERACTION_TO_NEXT_PAINT"),
        "fcp_s": field("FIRST_CONTENTFUL_PAINT_MS", 0.001),
        "overall": exp.get("overall_category"),
    } if fm else None

    # Opportunités : audits de type "opportunity" que le site ne réussit pas pleinement (score < 0.9)
    opps = []
    for aid, a in audits.items():
        d = a.get("details") or {}
        if d.get("type") == "opportunity" and a.get("score") is not None and a["score"] < 0.9:
            opps.append({"id": aid, "title": a.get("title"),
                         "savings_ms": round(d.get("overallSavingsMs") or 0),   # gain de temps estimé
                         "display_value": a.get("displayValue"),
                         "description": (a.get("description") or "")[:200]})
    opps.sort(key=lambda o: -o["savings_ms"])                                   # plus gros gain d'abord

    def quality(cat_id):
        """score /100 d'une catégorie (accessibility / best-practices) + ses 5 problèmes les plus lourds.
        Un problème = un contrôle Lighthouse réussi à moins de 90 %, de poids > 0 (les contrôles de poids 0
        sont seulement informatifs ou manuels). Retourne None si Google n'a pas renvoyé la catégorie."""
        cat = (lh.get("categories") or {}).get(cat_id)
        if not cat:
            return None
        raw = cat.get("score")
        sc = int(round(raw * 100)) if raw is not None else None
        issues = []
        for ref in cat.get("auditRefs", []):
            a = audits.get(ref.get("id")) or {}
            if (ref.get("weight", 0) > 0 and a.get("score") is not None and a["score"] < 0.9
                    and a.get("scoreDisplayMode") in ("binary", "numeric")):
                issues.append({"id": ref["id"], "title": a.get("title"),
                               "display_value": a.get("displayValue"), "weight": ref["weight"]})
        issues.sort(key=lambda i: -i["weight"])               # les plus importants d'abord
        return {"score": sc, "rating": _score_rating(sc), "issues": issues[:5]}

    return {"available": True, "score": score, "rating": _score_rating(score),
            "final_url": lh.get("finalUrl"), "lab": lab, "lab_rating": lab_rating,
            "field": field_data, "google_opportunities": opps[:5],
            "accessibility": quality("accessibility"), "best_practices": quality("best-practices"),
            "error": None}


# =================================================================== 3. FONCTION PRINCIPALE
def performance_site(url: str, strategies=("mobile", "desktop"), api_key: str | None = None) -> dict:
    """RÔLE : FONCTION PRINCIPALE du module, celle qu'appellent app.py et la ligne de commande,
    et dont recommendations.py / report.py lisent la sortie (ou le fichier JSON correspondant).
    Teste l'URL avec PageSpeed Insights pour chaque stratégie demandée.
    PARAMÈTRES :
      - url        : adresse du site (avec ou sans https://) ;
      - strategies : ('mobile', 'desktop') par défaut, ou une seule des deux ;
      - api_key    : clé API Google (sinon on lit la variable d'environnement PSI_API_KEY).
    RÈGLE : PageSpeed est indispensable. Au premier échec définitif d'une stratégie (après les
    nouveaux essais), on S'ARRÊTE : aucune mesure de remplacement, aucune estimation.
    SORTIE : le « contrat » décrit en haut du fichier (succès avec "summary", ou échec avec "error").
    100 % compatible JSON ; ne plante jamais."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    api_key = api_key or os.environ.get("PSI_API_KEY")
    out = {"url": url, "domain": _host(url), "measured_at": datetime.now().isoformat(timespec="seconds"),
           "api_available": False, "mobile": None, "desktop": None}

    for i, strat in enumerate(strategies):
        if i:
            time.sleep(2)                                  # petite pause : ménage le quota de l'API
        data, err = call_pagespeed(url, strat, api_key)
        if data:
            try:
                out[strat] = parse_pagespeed(data)
                continue                                   # cette stratégie est réussie : suivante
            except (KeyError, TypeError, AttributeError):
                err = "réponse de l'API illisible"
        # Échec : on s'arrête ici. On ne retourne QUE l'erreur (pas de chiffres à moitié valides).
        return {"url": url, "domain": out["domain"], "measured_at": out["measured_at"],
                "api_available": False, "failed_strategy": strat, "error": err}

    # Toutes les stratégies ont réussi : on prépare le résumé chiffré
    out["api_available"] = True
    m, d = out.get("mobile") or {}, out.get("desktop") or {}
    ratings = [r for r in (m.get("rating"), d.get("rating")) if r]
    order = {"bon": 0, "à améliorer": 1, "mauvais": 2}
    out["summary"] = {
        "mobile_score": m.get("score"), "desktop_score": d.get("score"),
        "mobile_lcp_s": (m.get("lab") or {}).get("lcp_s"),
        "mobile_cls": (m.get("lab") or {}).get("cls"),
        "mobile_tbt_ms": (m.get("lab") or {}).get("tbt_ms"),
        "worst_rating": max(ratings, key=order.get) if ratings else None,   # pire verdict de PERFORMANCE
        "mobile_accessibility": (m.get("accessibility") or {}).get("score"),
        "mobile_best_practices": (m.get("best_practices") or {}).get("score"),
        "desktop_accessibility": (d.get("accessibility") or {}).get("score"),
        "desktop_best_practices": (d.get("best_practices") or {}).get("score"),
    }
    return out


# =================================================================== LIGNE DE COMMANDE
def main() -> None:
    """RÔLE : lancer la mesure depuis le terminal et enregistrer results/<domaine>_performance.json.
    EXEMPLE : python -m src.performance https://www.exemple.ma --strategy both
    SUCCÈS : écrit le JSON et affiche une ligne par appareil.
    ÉCHEC DE L'API : affiche la cause, enregistre quand même le JSON d'erreur (pour qu'aucun ancien
    résultat périmé ne soit relu par les autres modules) et s'arrête avec le code 1.
    Aucune estimation n'est jamais faite."""
    ap = argparse.ArgumentParser(description="Mesure de performance (Google PageSpeed Insights).")
    ap.add_argument("url", help="adresse du site, ex. https://www.exemple.ma")
    ap.add_argument("--strategy", choices=["mobile", "desktop", "both"], default="both",
                    help="appareil(s) à tester (défaut : both)")
    ap.add_argument("--api-key", default=None, help="clé API PageSpeed (sinon variable PSI_API_KEY)")
    a = ap.parse_args()
    strategies = ("mobile", "desktop") if a.strategy == "both" else (a.strategy,)
    result = performance_site(a.url, strategies, a.api_key)

    out = Path("results") / f"{result['domain'].replace(':', '_')}_performance.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    if "summary" not in result:                            # échec : on s'arrête, sans rien inventer
        print(f"Mesure impossible ({result.get('failed_strategy')}) : {result.get('error')}")
        print("PageSpeed Insights est indispensable : aucune estimation n'a été faite.")
        print(f"-> {out}")
        sys.exit(1)
    for strat in strategies:
        b = result[strat]
        print(f"[{strat}] score {b['score']}/100 ({b['rating']}) | LCP {b['lab']['lcp_s']} s | "
              f"CLS {b['lab']['cls']} | TBT {b['lab']['tbt_ms']} ms")
        print(f"        accessibilité {(b.get('accessibility') or {}).get('score')}/100 | "
              f"bonnes pratiques {(b.get('best_practices') or {}).get('score')}/100")
    print(f"-> {out}")


if __name__ == "__main__":
    main()