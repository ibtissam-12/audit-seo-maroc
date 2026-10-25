"""
MODULE B : MOTEUR DE RECOMMANDATIONS (SEO technique + PageSpeed)
=============================================================================

CE QUE FAIT CE FICHIER (en bref)
--------------------------------
Il lit les deux résultats déjà produits par le groupe :
    - results/<site>.json              (audit.py)        -> problèmes de SEO technique
    - results/<site>_performance.json  (performance.py)  -> diagnostics Google PageSpeed :
                                                            performance, accessibilité, bonnes pratiques
puis il les COMBINE en une seule liste d'actions, sans doublons, classées par priorité
(haute, moyenne, basse), chacune avec : le problème chiffré, pourquoi il compte, quoi faire
précisément, l'effort estimé et des preuves (exemples d'URL, mesures Google).


COMMENT LES DOUBLONS SONT SUPPRIMÉS
-----------------------------------
    - Un même diagnostic PageSpeed vu sur mobile ET desktop devient UNE seule recommandation
      (la liste "devices" dit où il apparaît).
    - Un sujet détecté par audit.py ET par PageSpeed (ex. images sans alt, HTTPS, title,
      redirections, temps de réponse serveur) devient UNE seule recommandation : celle de l'audit,
      enrichie de la confirmation PageSpeed (sources = ["audit", "pagespeed"]).
    - Un filet de sécurité final supprime tout doublon d'identifiant.

COMMENT LA PRIORITÉ EST CALCULÉE (gravité + impact réel)
--------------------------------------------------------
Chaque recommandation reçoit une gravité interne de 1 à 4, puis la priorité affichée :
    gravité 3-4 -> "haute"  (4 = "blocking": empêche Google d'indexer ou de faire confiance au site)
    gravité 2   -> "moyenne"
    gravité 1   -> "basse"
L'IMPACT RÉEL ajuste la gravité :
    - SEO : proportion de pages touchées (un défaut sur 2 % des pages ≠ un défaut sur 60 %) ;
    - PageSpeed : mesure sur mobile prioritaire (indexation mobile-first) ; desktop seul = -1 ;
      données terrain (vrais visiteurs, CrUX) : "lent" = +1, "rapide" = -1 ;
      score de performance >= 90 : les simples opportunités restent "basse" ;
      accessibilité : poids du contrôle Lighthouse, +1 si le score de la catégorie est < 50.

Place dans l'architecture (projet à 3 personnes)
------------------------------------------------
    Personne 1 :  audit.py            -->  results/<site>.json
    Personne 2 :  performance.py      -->  results/<site>_performance.json
    Personne 2 :  recommendations.py  (CE FICHIER)  -->  results/<site>_recommendations.json
    Personne 3 :  report.py / app.py  lisent ce fichier (ou appellent generate_recommendations)

CONTRAT DE SORTIE 
-------------------------------------------------------------------------
    {
      "domain": "exemple.ma", "generated_at": "2026-10-25T14:30:00",
      "sources": {"audit": {"status": "ok|missing|error", "detail": ...},
                  "performance": {"status": "ok|missing|error", "detail": ...}},
      "health_score": 72,          # 0-100 ; None si l'audit SEO est absent (score non fiable)
      "grade": "C",                # A (>= 90) B (>= 75) C (>= 60) D (>= 40) E ; None si pas de score
      "counts": {"haute": 2, "moyenne": 4, "basse": 3},
      "quick_wins": ["missing_meta", ...],     # ids : priorité haute/moyenne ET effort faible
      "recommendations": [                      # DÉJÀ TRIÉES : haute -> moyenne -> basse
        {"rank": 1, "id", "topic", "category", "priority": "haute|moyenne|basse", "blocking": false,
         "severity": 3, "effort": "faible (< 1 h)", "effort_level": 1, "quick_win": true,
         "title", "problem", "why_it_matters", "action",
         "count": 12 ou None, "examples": [...], "evidence": [...],
         "sources": ["audit"] / ["pagespeed"] / ["audit", "pagespeed"],
         "devices": ["mobile", "desktop"], "learn_more": url ou None}
      ],
      "notes": ["avertissements de fiabilité"]
    }
    Si AUCUNE source n'est exploitable : {"domain", "generated_at", "sources", "error": "...",
    "recommendations": []} (et pas de "health_score").
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# ------------------------------------------------------------------ réglages
PRIORITY = {4: "haute", 3: "haute", 2: "moyenne", 1: "basse"}      # gravité interne -> priorité affichée
PRIORITY_ORDER = {"haute": 0, "moyenne": 1, "basse": 2}
EFFORT = {1: "faible (< 1 h)", 2: "moyen (1 journée)", 3: "élevé (plusieurs jours)"}
PENALTY = {"blocking": 15, "haute": 8, "moyenne": 4, "basse": 1}   # points retirés au score de santé
THIN_CONTENT_WORDS = 300          # en dessous : contenu jugé trop mince
HEAVY_HTML_KB = 500               # HTML anormalement lourd
MIN_OPPORTUNITY_MS = 300          # gain PageSpeed minimal pour qu'une opportunité mérite une action

# Sujets communs à l'audit et à PageSpeed : identifiant de contrôle Lighthouse -> sujet de l'audit.
# C'est la table qui décide de FUSIONNER au lieu de dupliquer.
PSI_OVERLAP = {
    "image-alt": "images_alt",            # images sans alt
    "is-on-https": "https",               # ressources non sécurisées / http
    "document-title": "title",            # page sans <title>
    "redirects": "redirects",             # redirections multiples
    "server-response-time": "server",     # temps de réponse du serveur
}


# =================================================================== UTILITAIRES
def _host(url: str) -> str:
    """RÔLE : nom de domaine sans 'www.' (même règle que audit.py et performance.py,
    pour retrouver les bons fichiers results/<domaine>*.json)."""
    h = urlparse(url if "//" in url else "//" + url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _fr(x, nd=1) -> str:
    """RÔLE : écrire un nombre à la française (virgule décimale). 4.8 -> '4,8' ; 310.0 -> '310'."""
    return f"{x:.{nd}f}".rstrip("0").rstrip(".").replace(".", ",") if nd else str(round(x))


def _sev_by_ratio(count: int, total: int, high=0.3, mid=0.1, cap=3) -> int:
    """RÔLE : adapter la gravité à l'ampleur du problème (impact réel côté SEO).
    SORTIE : 3 si >= 'high' des pages, 2 si >= 'mid', sinon 1 (plafonné à 'cap')."""
    ratio = count / total if total else 0
    return min(cap, 3 if ratio >= high else 2 if ratio >= mid else 1)


def _examples(pages: list, pred, n=3) -> list:
    """RÔLE : jusqu'à n URLs d'exemple répondant à la condition pred (pour retrouver le problème)."""
    return [p["url"] for p in pages if pred(p) and p.get("url")][:n]


def _rec(rid, category, severity, effort, title, problem, why, action, count=None, examples=None,
         topic=None, source="audit", evidence=None, devices=None, learn_more=None) -> dict:
    """RÔLE : fabriquer une recommandation au format commun (priorité fixée plus tard par _finalize).
    'topic' sert au dédoublonnage : deux recommandations du même sujet sont fusionnées."""
    return {"id": rid, "topic": topic or rid, "category": category, "severity": severity,
            "effort_level": effort, "effort": EFFORT[effort], "title": title, "problem": problem,
            "why_it_matters": why, "action": action, "count": count, "examples": examples or [],
            "evidence": evidence or [], "sources": [source], "devices": devices or [],
            "learn_more": learn_more}


def _finalize(r: dict) -> dict:
    """RÔLE : calculer les champs dérivés de la gravité (priorité, bloquante, gain rapide).
    Appelé APRÈS les fusions, car une fusion peut relever la gravité."""
    r["severity"] = max(1, min(4, r["severity"]))
    r["priority"] = PRIORITY[r["severity"]]
    r["blocking"] = r["severity"] >= 4
    r["quick_win"] = r["priority"] in ("haute", "moyenne") and r["effort_level"] == 1
    return r


# =================================================================== A. RÈGLES SEO (source : audit.py)
def _rules_access_and_indexation(audit, pages_ok) -> list:
    """RÔLE : ce qui décide si Google PEUT voir le site : robots.txt, noindex, sitemap, site en JavaScript.
    Thème le plus grave : un blocage ici rend inutile tout le reste."""
    recs, rb, sm, summ = [], audit.get("robots") or {}, audit.get("sitemap") or {}, audit.get("summary") or {}
    if rb.get("blocks_all"):
        recs.append(_rec("robots_blocks_all", "Indexation", 4, 1,
            "Le site est entièrement bloqué pour les robots",
            "robots.txt contient une règle 'Disallow: /' qui interdit tout le site.",
            "Google ne peut explorer aucune page : le site n'apparaît pas dans les résultats.",
            "Ouvrir /robots.txt, supprimer 'Disallow: /' (ou le limiter aux dossiers privés), puis "
            "demander une nouvelle exploration dans Google Search Console."))
    elif rb and rb.get("exists") is False:
        recs.append(_rec("robots_missing", "Indexation", 1, 1, "Fichier robots.txt absent",
            "Aucun robots.txt valide trouvé à la racine du site.",
            "Pas bloquant, mais il sert à guider les robots et à déclarer le sitemap.",
            "Créer /robots.txt avec 'User-agent: *', les dossiers à exclure (panier, compte) et la ligne "
            "'Sitemap: https://votre-site/sitemap.xml'."))
    if pages_ok and pages_ok[0].get("noindex"):
        recs.append(_rec("home_noindex", "Indexation", 4, 1,
            "La page d'accueil porte la balise noindex",
            "La page d'accueil demande explicitement à Google de ne PAS l'indexer.",
            "La page la plus importante du site disparaît des résultats de recherche.",
            "Retirer 'noindex' de la balise <meta name=\"robots\"> (souvent un réglage CMS « décourager les "
            "moteurs de recherche » resté actif après la mise en ligne).", examples=[pages_ok[0]["url"]]))
    elif pages_ok and summ.get("noindex_pages", 0) / len(pages_ok) >= 0.3:
        n = summ["noindex_pages"]
        recs.append(_rec("many_noindex", "Indexation", 3, 1, "Une grande part des pages est en noindex",
            f"{n} pages sur {len(pages_ok)} explorées sont exclues de l'index.",
            "Si ce n'est pas voulu, une grande partie du contenu ne peut pas générer de trafic.",
            "Vérifier page par page que le noindex est volontaire (panier, aide, filtres) et retirer celui des "
            "pages de contenu.", n, _examples(pages_ok, lambda p: p.get("noindex"))))
    if sm and sm.get("exists") is False:
        recs.append(_rec("sitemap_missing", "Indexation", 2, 1, "Aucun sitemap.xml trouvé",
            "Ni robots.txt ni les emplacements usuels n'indiquent de sitemap.",
            "Sans plan du site, Google découvre les pages plus lentement, surtout les pages profondes.",
            "Générer un sitemap.xml (plugin SEO du CMS) puis le soumettre dans Google Search Console."))
    elif sm.get("exists") and not sm.get("in_robots"):
        recs.append(_rec("sitemap_not_in_robots", "Indexation", 1, 1, "Sitemap non déclaré dans robots.txt",
            f"Le sitemap existe ({sm.get('url')}) mais robots.txt ne le mentionne pas.",
            "Les robots autres que Google (Bing...) le trouvent moins facilement.",
            f"Ajouter la ligne 'Sitemap: {sm.get('url')}' dans robots.txt."))
    if summ.get("js_rendered_suspected"):
        recs.append(_rec("js_rendered", "Indexation", 3, 3,
            "Site probablement généré en JavaScript : audit incomplet",
            "La page d'accueil téléchargée contient presque aucun texte ni lien.",
            "Les résultats de cet audit peuvent être sous-estimés, et Google peut indexer le contenu "
            "tardivement ou partiellement.",
            "Vérifier avec « Inspection d'URL » de Google Search Console ce que Google voit réellement ; "
            "envisager le rendu côté serveur (SSR) ou le pré-rendu des pages clés."))
    return recs


def _rules_security(audit) -> list:
    """RÔLE : HTTPS, redirection http->https, HSTS, validité du certificat.
    Google favorise HTTPS ; les navigateurs affichent « Non sécurisé » sinon (perte de confiance)."""
    recs, h = [], audit.get("https") or {}
    if not h:
        return recs
    if h.get("https_available") is False:
        recs.append(_rec("no_https", "Sécurité", 4, 2,
            "HTTPS indisponible ou certificat invalide" if h.get("cert_valid") is False else "Le site n'est pas en HTTPS",
            "Le site ne répond pas de façon sécurisée (HTTPS) ou son certificat est refusé.",
            "Les navigateurs affichent « Non sécurisé », les visiteurs partent, et Google favorise HTTPS.",
            "Installer un certificat valide (Let's Encrypt gratuit, souvent en un clic chez l'hébergeur) "
            "et forcer HTTPS sur tout le site.", topic="https"))
        return recs
    if h.get("cert_valid") is False:
        recs.append(_rec("cert_invalid", "Sécurité", 4, 1, "Certificat SSL invalide",
            "Le certificat est refusé par les navigateurs (expiré, mauvais domaine ou chaîne incomplète).",
            "Alerte de sécurité plein écran : la plupart des visiteurs quittent le site.",
            "Renouveler/réinstaller le certificat auprès de l'hébergeur et vérifier qu'il couvre bien le domaine."))
    days = h.get("cert_days_left")
    if days is not None and days < 30 and h.get("cert_valid") is not False:
        recs.append(_rec("cert_expiring", "Sécurité", 4 if days <= 7 else 3, 1,
            "Le certificat SSL expire bientôt", f"Il reste {days} jour(s) avant expiration.",
            "À l'expiration, le site affiche une alerte de sécurité bloquante.",
            "Renouveler le certificat maintenant et activer le renouvellement automatique."))
    if h.get("http_redirects_to_https") is False:
        recs.append(_rec("http_no_redirect", "Sécurité", 3, 1,
            "La version http:// ne redirige pas vers https://",
            "Le site reste accessible en http non sécurisé.",
            "Google voit deux versions du site (contenu dupliqué) et les visiteurs peuvent arriver en non sécurisé.",
            "Ajouter une redirection permanente 301 de http vers https (fichier .htaccess, config Nginx ou réglage CDN).",
            topic="https"))
    if h.get("hsts") is False:
        recs.append(_rec("no_hsts", "Sécurité", 1, 1, "En-tête HSTS absent",
            "Le serveur n'impose pas HTTPS aux navigateurs.",
            "Petit gain de sécurité ; protège contre les retours en http.",
            "Ajouter l'en-tête Strict-Transport-Security (par ex. max-age=31536000) une fois HTTPS stable."))
    return recs


def _rules_content(idx) -> list:
    """RÔLE : balises que Google lit pour comprendre et afficher chaque page : title, meta description,
    H1, doublons, canonical, mobile (viewport), contenu mince, images sans alt.
    Les comptages portent sur les pages INDEXABLES uniquement (comme audit.py)."""
    recs, n = [], len(idx)
    if not n:
        return recs

    def count(pred):
        return sum(1 for p in idx if pred(p))

    def absent(p, key):                    # clé présente mais vide (une clé absente = inconnu : on ne l'accuse pas)
        return key in p and not p[key]

    c = count(lambda p: absent(p, "title"))
    if c:
        recs.append(_rec("missing_title", "Contenu", 4 if c / n >= 0.1 else 3, 1,
            "Pages sans balise <title>", f"{c} page(s) sur {n} n'ont pas de title.",
            "Le title est le premier critère de classement et le titre cliquable dans Google. "
            "Sans lui, Google en invente un.",
            "Rédiger un title unique (30-60 caractères) avec le mot-clé principal en début de phrase.",
            c, _examples(idx, lambda p: absent(p, "title")), topic="title"))
    titles = {}
    for p in idx:
        if p.get("title"):
            titles.setdefault(p["title"], []).append(p.get("url"))
    c = sum(len(u) for u in titles.values() if len(u) > 1)
    if c:
        worst = max(titles.items(), key=lambda kv: len(kv[1]))
        recs.append(_rec("duplicate_title", "Contenu", _sev_by_ratio(c, n), 2, "Titles en double",
            f"{c} page(s) partagent un title identique à une autre "
            f"(le plus répété : « {worst[0][:50]} » sur {len(worst[1])} pages).",
            "Google ne sait pas quelle page afficher pour une recherche : elles se concurrencent entre elles.",
            "Donner à chaque page un title spécifique à son contenu (nom du produit/service + marque en fin).",
            c, worst[1][:3]))
    c = count(lambda p: p.get("title_len", 0) > 60)
    if c:
        recs.append(_rec("title_too_long", "Contenu", 1, 1, "Titles trop longs (> 60 caractères)",
            f"{c} title(s) dépassent 60 caractères.", "Google tronque l'affichage avec « ... » : le message est coupé.",
            "Raccourcir à 60 caractères en gardant le mot-clé au début.", c,
            _examples(idx, lambda p: p.get("title_len", 0) > 60)))
    c = count(lambda p: 0 < p.get("title_len", 0) < 30)
    if c:
        recs.append(_rec("title_too_short", "Contenu", 1, 1, "Titles trop courts (< 30 caractères)",
            f"{c} title(s) font moins de 30 caractères.", "Occasion manquée d'ajouter des mots-clés utiles.",
            "Compléter avec le bénéfice principal ou la localisation (ex. « ... à Fès »).", c,
            _examples(idx, lambda p: 0 < p.get("title_len", 0) < 30)))

    c = count(lambda p: absent(p, "meta_desc"))
    if c:
        recs.append(_rec("missing_meta", "Contenu", _sev_by_ratio(c, n), 2, "Pages sans meta description",
            f"{c} page(s) sur {n} n'ont pas de meta description.",
            "C'est le texte affiché sous le titre dans Google : il influence le taux de clic.",
            "Écrire 70-160 caractères uniques par page, avec le mot-clé et un appel à l'action.", c,
            _examples(idx, lambda p: absent(p, "meta_desc"))))
    metas = {}
    for p in idx:
        if p.get("meta_desc"):
            metas.setdefault(p["meta_desc"], []).append(p.get("url"))
    c = sum(len(u) for u in metas.values() if len(u) > 1)
    if c:
        recs.append(_rec("duplicate_meta", "Contenu", _sev_by_ratio(c, n, cap=2), 2,
            "Meta descriptions en double", f"{c} page(s) partagent la même meta description.",
            "Des extraits identiques dans Google donnent peu de raisons de cliquer.",
            "Personnaliser la meta description de chaque page (ou la laisser vide plutôt que dupliquée).", c,
            max(metas.values(), key=len)[:3]))
    c = count(lambda p: p.get("meta_len", 0) > 160 or 0 < p.get("meta_len", 0) < 70)
    if c:
        recs.append(_rec("meta_length", "Contenu", 1, 1, "Meta descriptions trop longues ou trop courtes",
            f"{c} meta description(s) hors de la plage 70-160 caractères.",
            "Trop long : coupé par Google. Trop court : extrait peu convaincant.", "Viser 120-155 caractères.", c,
            _examples(idx, lambda p: p.get("meta_len", 0) > 160 or 0 < p.get("meta_len", 0) < 70)))

    c = count(lambda p: p.get("h1_count", 1) == 0)
    if c:
        recs.append(_rec("missing_h1", "Contenu", _sev_by_ratio(c, n), 1, "Pages sans titre principal (H1)",
            f"{c} page(s) sur {n} n'ont aucun H1.",
            "Le H1 indique à Google (et aux lecteurs d'écran) le sujet principal de la page.",
            "Ajouter un seul H1 clair par page, cohérent avec le title.", c,
            _examples(idx, lambda p: p.get("h1_count", 1) == 0)))
    c = count(lambda p: p.get("h1_count", 0) > 1)
    if c:
        recs.append(_rec("multiple_h1", "Contenu", 1, 1, "Pages avec plusieurs H1", f"{c} page(s) ont plus d'un H1.",
            "Brouille le sujet principal ; défaut mineur.",
            "Garder un seul H1 et passer les autres titres en H2/H3.", c,
            _examples(idx, lambda p: p.get("h1_count", 0) > 1)))

    c = count(lambda p: p.get("viewport", True) is False)
    if c:
        recs.append(_rec("no_viewport", "Mobile", 3, 1, "Balise viewport absente (site non adapté au mobile)",
            f"{c} page(s) n'ont pas de balise viewport.",
            "Google indexe d'abord la version mobile : sans viewport la page s'affiche en version « bureau » minuscule.",
            "Ajouter <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"> dans le <head> "
            "et vérifier que le thème est responsive.", c, _examples(idx, lambda p: p.get("viewport", True) is False)))
    c = count(lambda p: absent(p, "canonical"))
    if c:
        recs.append(_rec("missing_canonical", "Contenu", 2 if c / n >= 0.5 else 1, 1,
            "Balise canonical absente", f"{c} page(s) sur {n} n'ont pas de canonical.",
            "Sans canonical, les variantes d'une même page (paramètres, filtres) peuvent être comptées comme doublons.",
            "Ajouter <link rel=\"canonical\"> pointant vers l'URL officielle de chaque page.", c,
            _examples(idx, lambda p: absent(p, "canonical"))))
    c = count(lambda p: 0 < p.get("word_count", 0) < THIN_CONTENT_WORDS)
    if c:
        recs.append(_rec("thin_content", "Contenu", _sev_by_ratio(c, n, high=0.4, cap=2), 3,
            "Pages au contenu très court", f"{c} page(s) ont moins de {THIN_CONTENT_WORDS} mots.",
            "Peu de texte = peu de raisons pour Google de classer la page sur des recherches variées.",
            "Enrichir les pages importantes (description détaillée, FAQ, guides) ; fusionner ou noindexer les pages vides.",
            c, _examples(idx, lambda p: 0 < p.get("word_count", 0) < THIN_CONTENT_WORDS)))
    alt = sum(p.get("images_missing_alt", 0) for p in idx)
    total_img = sum(p.get("images_total", 0) for p in idx)
    if alt:
        recs.append(_rec("images_alt", "Images", 2 if alt >= 10 else 1, 2, "Images sans texte alternatif",
            f"{alt} image(s) sur {total_img} n'ont pas d'attribut alt.",
            "Google Images et l'accessibilité reposent sur l'alt : trafic image perdu et site moins accessible.",
            "Décrire chaque image utile en une phrase courte ; mettre alt=\"\" pour les images purement décoratives.",
            alt, _examples(idx, lambda p: p.get("images_missing_alt", 0) > 0), topic="images_alt"))
    return recs


def _rules_links_and_errors(audit, pages) -> list:
    """RÔLE : liens cassés, pages en erreur, chaînes de redirections, HTML lourd.
    Un lien interne cassé gaspille le « budget d'exploration » de Google et frustre le visiteur."""
    recs, broken = [], audit.get("broken_links") or []
    internal = [b for b in broken if b.get("category") == "broken" and b.get("internal")]
    external = [b for b in broken if b.get("category") == "broken" and not b.get("internal")]
    if internal:
        recs.append(_rec("broken_internal", "Liens", 3, 1, "Liens internes cassés",
            f"{len(internal)} lien(s) interne(s) mènent à une erreur (404/410/5xx).",
            "Le visiteur tombe sur une impasse et Google perd du temps sur des pages inexistantes.",
            "Corriger le lien sur la page indiquée, ou créer une redirection 301 vers la page de remplacement.",
            len(internal), [f"{b.get('url')} (trouvé sur {b.get('found_on')})" for b in internal[:3]]))
    if external:
        recs.append(_rec("broken_external", "Liens", 2 if len(external) >= 5 else 1, 1, "Liens externes cassés",
            f"{len(external)} lien(s) sortant(s) pointent vers des pages disparues.",
            "Mauvais signal de qualité et mauvaise expérience pour le lecteur.",
            "Remplacer par une source à jour ou supprimer le lien.", len(external),
            [f"{b.get('url')} (trouvé sur {b.get('found_on')})" for b in external[:3]]))
    errs = [p for p in pages if p.get("status") is None or p["status"] >= 400]
    if errs:
        recs.append(_rec("pages_error", "Liens", 3, 2, "Pages explorées en erreur",
            f"{len(errs)} page(s) du site répondent avec une erreur ou pas du tout.",
            "Ces pages ne peuvent pas être indexées ; si elles sont liées, elles dégradent l'expérience.",
            "Réparer la page ou la rediriger (301) ; retirer les liens qui y mènent.", len(errs),
            [p["url"] for p in errs[:3] if p.get("url")]))
    chains = [p for p in pages if p.get("redirects", 0) >= 2]
    if chains:
        recs.append(_rec("redirect_chains", "Technique", 2 if len(chains) >= 5 else 1, 2, "Chaînes de redirections",
            f"{len(chains)} page(s) passent par 2 redirections ou plus avant d'arriver.",
            "Chaque saut ralentit l'accès et dilue légèrement la valeur SEO des liens.",
            "Faire pointer les liens directement vers l'URL finale ; une seule redirection 301 au maximum.",
            len(chains), [p["url"] for p in chains[:3]], topic="redirects"))
    heavy = [p for p in pages if p.get("size_kb", 0) > HEAVY_HTML_KB]
    if heavy:
        recs.append(_rec("heavy_html", "Technique", 1, 2, "Pages HTML très lourdes",
            f"{len(heavy)} page(s) dépassent {HEAVY_HTML_KB} Ko de HTML.",
            "Un HTML énorme (souvent du code ou des données inutiles) ralentit l'exploration.",
            "Nettoyer les scripts/styles intégrés, paginer les longues listes.", len(heavy),
            [p["url"] for p in heavy[:3]]))
    return recs


def _rule_slow_server(audit) -> list:
    """RÔLE : temps de réponse du serveur sur TOUTES les pages explorées (PageSpeed n'en teste qu'une)."""
    summ = audit.get("summary") or {}
    ok = [p for p in audit.get("pages") or [] if p.get("status") == 200 and "ttfb_s" in p]
    slow = [p for p in ok if p["ttfb_s"] > 1.5]
    if ok and (len(slow) / len(ok) >= 0.3 or (summ.get("avg_ttfb_s") or 0) > 0.8):
        return [_rec("slow_server", "Performance", 3 if len(slow) / len(ok) >= 0.3 else 2, 3,
            "Serveur lent sur l'ensemble du site",
            f"Temps de réponse moyen {summ.get('avg_ttfb_s')} s ; {len(slow)} page(s) sur {len(ok)} dépassent 1,5 s.",
            "Un serveur lent limite le nombre de pages que Google explore et pénalise toutes les pages, "
            "pas seulement celle testée par PageSpeed.",
            "Activer un cache serveur/CMS, utiliser un CDN, vérifier l'offre d'hébergement et les requêtes "
            "base de données lentes.", len(slow), [p["url"] for p in slow[:3]], topic="server")]
    return []


# =================================================================== B. RÈGLES PAGESPEED (source : performance.py)
# Core Web Vitals et mesures : clé -> (libellé, métrique "cœur" ?, seuil "bon", pourquoi, action concrète)
METRICS = {
    "lcp_s": ("Affichage du contenu principal (LCP)", True, "≤ 2,5 s",
              "Core Web Vital : mesure quand le plus gros élément (image principale, titre) devient visible. "
              "C'est un critère de classement Google (expérience de page).",
              "Identifier l'élément LCP (souvent la bannière ou la photo principale), le compresser et le servir "
              "en WebP/AVIF aux bonnes dimensions, ne pas le charger en lazy-loading, le précharger "
              "(<link rel=\"preload\">), et réduire le temps de réponse du serveur."),
    "cls": ("Stabilité visuelle (CLS)", True, "≤ 0,1",
            "Core Web Vital : la page « saute » pendant le chargement, ce qui provoque des clics ratés et "
            "dégrade l'expérience de page.",
            "Définir width et height sur toutes les images, vidéos et iframes, réserver l'espace des bannières "
            "et publicités, ne jamais insérer de contenu au-dessus du contenu déjà affiché, utiliser "
            "font-display: swap avec une police de remplacement proche."),
    "tbt_ms": ("Blocage de la page par le JavaScript (TBT)", True, "≤ 200 ms",
               "Le TBT mesure combien de temps la page est figée pendant le chargement ; c'est l'indicateur de "
               "laboratoire de la réactivité (INP), un Core Web Vital.",
               "Supprimer les scripts tiers inutiles (chat, pixels, widgets), charger les scripts restants en "
               "async/defer, découper les gros fichiers JavaScript et ne charger que le code nécessaire à la page."),
    "fcp_s": ("Premier affichage (FCP)", False, "≤ 1,8 s",
              "Temps avant que le visiteur voie quelque chose ; un écran blanc long fait partir les visiteurs.",
              "Intégrer le CSS critique dans le HTML, différer les CSS/JS bloquants et alléger les polices web."),
    "speed_index_s": ("Vitesse d'affichage visuel (Speed Index)", False, "≤ 3,4 s",
                      "Mesure la rapidité avec laquelle la page se remplit visuellement.",
                      "Réduire le poids des images au-dessus de la ligne de flottaison et différer le contenu "
                      "non visible au premier écran."),
}
METRIC_UNITS = {"lcp_s": " s", "fcp_s": " s", "speed_index_s": " s", "tbt_ms": " ms", "cls": ""}
FIELD_FR = {"FAST": "rapide", "AVERAGE": "moyen", "SLOW": "lent"}   # catégories CrUX de Google
METRIC_FIELD = {"lcp_s": "lcp_s", "cls": "cls", "tbt_ms": "inp_ms"}   # mesure de laboratoire -> mesure terrain équivalente

# Actions concrètes pour les contrôles Lighthouse les plus fréquents (sinon : texte même de Google)
OPP_ACTIONS = {
    "render-blocking-resources": "Différer ou intégrer en ligne les CSS/JS qui bloquent l'affichage (attributs async/defer, CSS critique).",
    "unused-css-rules": "Supprimer le CSS inutilisé (purge des feuilles de style, retirer les thèmes/plugins non utilisés).",
    "unused-javascript": "Supprimer ou différer le JavaScript non utilisé (retirer les plugins inutiles, découper les bundles).",
    "uses-optimized-images": "Compresser les images (qualité 75-85 %) avec un outil ou un plugin d'optimisation automatique.",
    "modern-image-formats": "Convertir les images JPEG/PNG en WebP ou AVIF.",
    "uses-responsive-images": "Servir des images aux dimensions réellement affichées (attribut srcset / tailles multiples).",
    "offscreen-images": "Activer le chargement différé (loading=\"lazy\") des images situées sous le premier écran.",
    "unminified-css": "Minifier les fichiers CSS (plugin de cache/minification ou outil de build).",
    "unminified-javascript": "Minifier les fichiers JavaScript (plugin de cache/minification ou outil de build).",
    "uses-text-compression": "Activer la compression gzip ou Brotli sur le serveur pour HTML, CSS et JavaScript.",
    "server-response-time": "Réduire le temps de réponse du serveur : cache de pages, CDN, optimisation des requêtes base de données.",
    "redirects": "Faire pointer les liens directement vers l'URL finale et supprimer les redirections inutiles.",
    "uses-rel-preconnect": "Ajouter <link rel=\"preconnect\"> pour les 1-2 domaines tiers les plus importants (polices, CDN).",
    "duplicated-javascript": "Supprimer les modules JavaScript en double dans les bundles.",
    "legacy-javascript": "Ne plus envoyer de JavaScript transpilé pour d'anciens navigateurs (cibler les navigateurs modernes).",
    "total-byte-weight": "Réduire le poids total de la page : images, polices et scripts les plus lourds en premier.",
    "efficient-animated-content": "Remplacer les GIF animés par des vidéos MP4/WebM.",
}
ACC_ACTIONS = {
    "image-alt": "Ajouter un attribut alt descriptif à chaque image utile (alt=\"\" pour les images décoratives).",
    "color-contrast": "Augmenter le contraste entre texte et fond : au moins 4,5:1 (3:1 pour les grands textes).",
    "label": "Associer un <label for=\"...\"> visible à chaque champ de formulaire.",
    "button-name": "Donner à chaque bouton un texte visible ou un attribut aria-label.",
    "link-name": "Donner à chaque lien un texte explicite (éviter les liens ou icônes sans texte).",
    "html-has-lang": "Ajouter la langue sur la balise racine : <html lang=\"fr\">.",
    "document-title": "Ajouter une balise <title> descriptive à la page.",
    "heading-order": "Respecter l'ordre des titres (H1, puis H2, puis H3) sans sauter de niveau.",
    "meta-viewport": "Ne pas désactiver le zoom : retirer user-scalable=no et maximum-scale de la balise viewport.",
    "target-size": "Agrandir les boutons et liens tactiles (au moins 24×24 px) ou les espacer davantage.",
    "aria-allowed-attr": "Retirer ou corriger les attributs ARIA non autorisés sur les éléments concernés.",
    "frame-title": "Ajouter un attribut title à chaque iframe.",
    "list": "Utiliser de vraies listes (<ul>/<ol> avec <li>) pour les éléments de liste.",
}
BP_ACTIONS = {
    "is-on-https": "Charger toutes les ressources (images, scripts, CSS) en https:// et corriger les contenus mixtes.",
    "no-vulnerable-libraries": "Mettre à jour les bibliothèques JavaScript signalées vers leur dernière version corrigée.",
    "errors-in-console": "Ouvrir la console du navigateur (F12), corriger les erreurs JavaScript et les ressources introuvables.",
    "deprecations": "Remplacer les fonctions obsolètes signalées par leurs équivalents actuels.",
    "image-aspect-ratio": "Afficher les images à leur proportion d'origine (définir width/height cohérents).",
    "image-size-responsive": "Fournir des images de résolution adaptée à l'écran (srcset).",
    "doctype": "Ajouter <!DOCTYPE html> tout en haut du document.",
    "charset": "Déclarer l'encodage : <meta charset=\"utf-8\"> au début du <head>.",
    "inspector-issues": "Corriger les problèmes listés dans l'onglet « Problèmes » des outils de développement Chrome.",
    "third-party-cookies": "Limiter les services tiers qui déposent des cookies tiers (bientôt bloqués par Chrome).",
}
BP_SECURITY = {"is-on-https", "no-vulnerable-libraries"}          # contrôles de sécurité : gravité plus forte


def _usable_devices(perf) -> list:
    """RÔLE : lister les appareils exploitables de la sortie de performance.py : [('mobile', bloc), ('desktop', bloc)].
    Un appareil est exploitable si son bloc existe et porte available=True (jamais de chiffre inventé)."""
    out = []
    for d in ("mobile", "desktop"):
        b = (perf or {}).get(d)
        if isinstance(b, dict) and b.get("available"):
            out.append((d, b))
    return out


def _learn_more(kind: str, audit_id: str) -> str:
    """RÔLE : lien vers la fiche officielle Lighthouse d'un contrôle (kind : performance, accessibility, best-practices)."""
    return f"https://developer.chrome.com/docs/lighthouse/{kind}/{audit_id}/"


def _rules_pagespeed_performance(devs) -> list:
    """RÔLE : transformer les mesures de PageSpeed en recommandations de VITESSE.
    1) une recommandation par mesure qui n'est pas « bon » (LCP, CLS, TBT = Core Web Vitals ; FCP et Speed Index
       en second plan), avec ajustement par l'IMPACT RÉEL : mobile prioritaire, données terrain des vrais visiteurs ;
    2) la réactivité réelle (INP) si les vrais visiteurs la jugent mauvaise ;
    3) les opportunités de gain de temps proposées par Google (>= 300 ms), fusionnées entre mobile et desktop ;
    4) si le score est < 90 mais qu'aucun détail n'est disponible : une recommandation de synthèse."""
    recs = []
    if not devs:
        return recs
    mobile = dict(devs).get("mobile")
    ref_block = mobile or devs[0][1]
    perf_score = ref_block.get("score")
    field = next((b.get("field") for _, b in devs if b.get("field")), None)   # terrain : mobile d'abord

    # --- 1) mesures de laboratoire
    for key, (label, core, good, why, action) in METRICS.items():
        sevs, evid, failing, measures = [], [], [], []
        for dev, b in devs:
            val, rating = (b.get("lab") or {}).get(key), (b.get("lab_rating") or {}).get(key)
            if val is None or rating in (None, "bon"):
                continue
            s = (3 if rating == "mauvais" else 2) if core else (2 if rating == "mauvais" else 1)
            sevs.append(s if dev == "mobile" else max(1, s - 1))             # desktop seul = moins grave
            failing.append(dev)
            measures.append(f"{dev} {_fr(val, 2 if key == 'cls' else 1)}{METRIC_UNITS[key]} ({rating})")
        if not sevs:
            continue
        evid = list(measures)
        sev = max(sevs)
        f = ((field or {}).get(METRIC_FIELD.get(key)) or {}) if field else {}
        if f.get("category") == "SLOW":
            sev = min(3, sev + 1)
            evid.append("confirmé par les vrais visiteurs de Chrome (données terrain : lent)")
        elif f.get("category") == "FAST":
            sev = max(1, sev - 1)
            evid.append("nuance : les vrais visiteurs de Chrome ont une expérience rapide (données terrain)")
        recs.append(_rec(f"perf_{key}", "Performance", sev, 3 if core else 2,
            f"{label} à améliorer", f"{label} : " + " ; ".join(measures) + f" (seuil « bon » : {good}).", why, action,
            source="pagespeed", evidence=evid, devices=failing, topic=f"perf_{key}"))

    # --- 2) INP mesuré chez de vrais visiteurs (seule source de l'INP : les données terrain)
    inp = (field or {}).get("inp_ms") if field else None
    if inp and inp.get("category") in ("AVERAGE", "SLOW"):
        recs.append(_rec("perf_inp", "Performance", 3 if inp["category"] == "SLOW" else 2, 3,
            "Réactivité aux clics à améliorer (INP)",
            f"INP mesuré chez de vrais visiteurs : {_fr(inp.get('value', 0), 0)} ms ({FIELD_FR.get(inp['category'], inp['category'])}).",
            "Core Web Vital : délai entre un clic et la réaction de la page ; critère de classement Google.",
            METRICS["tbt_ms"][4], source="pagespeed", devices=[d for d, _ in devs],
            evidence=["données terrain Chrome (CrUX)"], topic="perf_inp"))

    # --- 3) opportunités de Google, fusionnées entre appareils
    merged = {}
    for dev, b in devs:
        for o in b.get("google_opportunities") or []:
            if not o.get("id") or (o.get("savings_ms") or 0) < MIN_OPPORTUNITY_MS:
                continue
            m = merged.setdefault(o["id"], {"o": o, "devices": [], "savings": 0})
            m["devices"].append(dev)
            m["savings"] = max(m["savings"], o.get("savings_ms") or 0)
    for oid, m in merged.items():
        o, sav = m["o"], m["savings"]
        sev = 3 if sav >= 1500 else 2 if sav >= 800 else 1
        if isinstance(perf_score, (int, float)) and perf_score >= 90:
            sev = 1                                                          # page déjà rapide : simple optimisation
        desc = (o.get("description") or "").strip()
        action = OPP_ACTIONS.get(oid) or (f"Appliquer la correction détaillée par Google : {desc}" if desc
                                          else f"Corriger « {o.get('title')} » en suivant la fiche Lighthouse.")
        recs.append(_rec(f"psi_{oid}", "Performance", sev, 2, str(o.get("title") or oid),
            f"{o.get('title')} : gain estimé {_fr(sav / 1000)} s" + (f" ({o['display_value']})" if o.get("display_value") else "") + ".",
            f"Google estime que corriger ce point accélère le chargement d'environ {_fr(sav / 1000)} s ; "
            "une page plus rapide améliore l'expérience mobile et le classement.",
            action, source="pagespeed", devices=m["devices"], evidence=[f"gain estimé : {sav} ms"],
            learn_more=_learn_more("performance", oid), topic=PSI_OVERLAP.get(oid, f"psi_{oid}")))

    # --- 4) synthèse si score < 90 sans aucun détail exploitable
    if not recs and isinstance(perf_score, (int, float)) and perf_score < 90:
        recs.append(_rec("perf_score", "Performance", 3 if perf_score < 50 else 2, 3,
            f"Performance à améliorer ({perf_score}/100 selon Google)",
            f"Score PageSpeed : {perf_score}/100 sans détail disponible.",
            "La vitesse est un critère de classement et influence le taux de rebond.",
            "Relancer l'analyse sur https://pagespeed.web.dev pour obtenir la liste détaillée des correctifs.",
            source="pagespeed", devices=[d for d, _ in devs], topic="perf_score"))
    return recs


def _rules_pagespeed_quality(devs, block_key, kind, category, actions) -> list:
    """RÔLE : transformer les problèmes d'ACCESSIBILITÉ ou de BONNES PRATIQUES signalés par Lighthouse
    en recommandations. Un même contrôle vu sur mobile et desktop = UNE seule recommandation.
    PARAMÈTRES : block_key ('accessibility' / 'best_practices'), kind (dossier de la doc Lighthouse),
    category (libellé affiché), actions (dictionnaire d'actions concrètes connues).
    GRAVITÉ : accessibilité = selon le poids du contrôle (>= 7 : moyenne, sinon basse), +1 si le score de la
    catégorie est < 50 ; bonnes pratiques = haute pour la sécurité (HTTPS, bibliothèques vulnérables),
    moyenne si poids >= 5, sinon basse."""
    merged = {}
    for dev, b in devs:
        q = b.get(block_key) or {}
        for it in q.get("issues") or []:
            if not it.get("id"):
                continue
            m = merged.setdefault(it["id"], {"it": it, "devices": [], "score": None})
            m["devices"].append(dev)
            if isinstance(q.get("score"), (int, float)):
                m["score"] = q["score"] if m["score"] is None else min(m["score"], q["score"])
    recs = []
    for iid, m in merged.items():
        it, w, cat_score = m["it"], it_weight(m["it"]), m["score"]
        if block_key == "accessibility":
            sev = 2 if w >= 7 else 1
            if cat_score is not None and cat_score < 50:
                sev = min(3, sev + 1)
        else:
            sev = 3 if iid in BP_SECURITY else 2 if w >= 5 else 1
        title = str(it.get("title") or iid)
        action = actions.get(iid) or f"Corriger le point « {title} » en suivant la fiche officielle Lighthouse (lien ci-joint)."
        detail = f" ({it['display_value']})" if it.get("display_value") else ""
        recs.append(_rec(f"psi_{iid}", category, sev, 2, title,
            f"Lighthouse signale : {title}{detail}."
            + (f" Score {category.lower()} : {cat_score}/100." if cat_score is not None else ""),
            "Corriger ce contrôle améliore l'expérience de tous les visiteurs et la fiabilité technique du site."
            if block_key != "accessibility" else
            "Un site peu accessible exclut une partie des visiteurs (malvoyants, navigation au clavier) "
            "et dégrade l'expérience que Google cherche à mesurer.",
            action, source="pagespeed", devices=m["devices"],
            evidence=[f"contrôle Lighthouse « {iid} », poids {w}"], learn_more=_learn_more(kind, iid),
            topic=PSI_OVERLAP.get(iid, f"psi_{iid}")))
    return recs


def it_weight(item: dict) -> int:
    """RÔLE : poids (importance) d'un contrôle Lighthouse, 0 si absent."""
    w = item.get("weight")
    return w if isinstance(w, (int, float)) else 0


# =================================================================== C. FUSION ET DÉDOUBLONNAGE
def _merge(audit_recs: list, psi_recs: list) -> list:
    """RÔLE : combiner les deux sources SANS doublon.
    - si un sujet PageSpeed existe déjà côté audit (même 'topic'), on n'ajoute PAS une 2e recommandation :
      on enrichit celle de l'audit (sources, appareils, preuves) et on garde la gravité la plus haute ;
    - deux recommandations PageSpeed du même sujet sont fusionnées de la même manière ;
    - filet final : un identifiant ne peut apparaître qu'une fois.
    SORTIE : liste fusionnée (non triée)."""
    out, by_topic = [], {}
    for r in list(audit_recs) + list(psi_recs):
        host = by_topic.get(r["topic"])
        if host is None:
            out.append(r)
            by_topic[r["topic"]] = r
            continue
        host["sources"] = sorted(set(host["sources"]) | set(r["sources"]))
        host["devices"] = sorted(set(host["devices"]) | set(r["devices"]))
        host["evidence"] = host["evidence"] + [e for e in r["evidence"] if e not in host["evidence"]]
        if r["sources"] == ["pagespeed"] and "audit" in host["sources"]:
            host["evidence"].append(f"PageSpeed signale aussi (page testée) : {r['problem'].replace('Lighthouse signale : ', '')}")
        host["severity"] = max(host["severity"], r["severity"])
        host["learn_more"] = host["learn_more"] or r["learn_more"]
    final, seen = [], set()
    for r in out:                                   # filet de sécurité : un seul exemplaire par identifiant
        if r["id"] not in seen:
            seen.add(r["id"])
            final.append(r)
    return final


# =================================================================== D. LECTURE DES SOURCES
def _source_status(data, kind: str) -> dict:
    """RÔLE : dire si une source est exploitable.
    SORTIE : {"status": "ok" | "missing" | "error", "detail": message ou None}
    - audit.py : OK s'il a une clé "summary" ; en erreur s'il porte la clé "error" (site injoignable) ;
    - performance.py : OK si api_available est vrai et qu'au moins un appareil est exploitable ;
      en erreur sinon (le message de performance.py est repris tel quel, jamais remplacé par une estimation)."""
    if data is None:
        return {"status": "missing", "detail": "fichier absent ou illisible"}
    if not isinstance(data, dict):
        return {"status": "error", "detail": "format inattendu"}
    if kind == "audit":
        if "error" in data or "summary" not in data:
            return {"status": "error", "detail": data.get("error") or "audit incomplet (pas de summary)"}
        return {"status": "ok", "detail": data.get("audited_at")}
    if data.get("api_available") and _usable_devices(data):
        return {"status": "ok", "detail": data.get("measured_at")}
    return {"status": "error", "detail": data.get("error") or "aucune mesure PageSpeed exploitable"}


def _safe(label: str, fn, notes: list, *args):
    """RÔLE : exécuter un groupe de règles sans jamais planter. Si une règle échoue (donnée inattendue),
    on l'ignore, on le note dans "notes", et les autres règles continuent."""
    try:
        return fn(*args)
    except Exception as e:                                 # volontairement large : un bug ne doit pas tout bloquer
        notes.append(f"Analyse « {label} » ignorée (erreur interne : {type(e).__name__}).")
        return []


# =================================================================== E. FONCTION PRINCIPALE
def generate_recommendations(audit: dict | None, perf: dict | None = None) -> dict:
    """RÔLE : FONCTION PRINCIPALE du module (appelée par app.py, report.py et la ligne de commande).
    PARAMÈTRES : audit = sortie de audit_site() ; perf = sortie de performance_site() (l'un ou l'autre peut
    être None, absent ou en erreur : on produit ce que les données permettent).
    ÉTAPES : 1) évaluer l'état des sources ; 2) appliquer les règles SEO et PageSpeed ; 3) fusionner et
    supprimer les doublons ; 4) calculer priorité et gain rapide ; 5) trier ; 6) calculer le score de santé.
    SORTIE : le « contrat » décrit en haut du fichier. Ne plante jamais."""
    notes: list = []
    sources = {"audit": _source_status(audit, "audit"), "performance": _source_status(perf, "performance")}
    domain = (audit or {}).get("domain") if isinstance(audit, dict) else None
    domain = domain or ((perf or {}).get("domain") if isinstance(perf, dict) else None)
    base = {"domain": domain, "generated_at": datetime.now().isoformat(timespec="seconds"), "sources": sources}

    a_ok, p_ok = sources["audit"]["status"] == "ok", sources["performance"]["status"] == "ok"

    # Cas particulier : site injoignable (l'audit l'a constaté) -> une seule recommandation, rien d'autre à dire
    if sources["audit"]["status"] == "error" and isinstance(audit, dict) and audit.get("access"):
        r = _finalize(_rec("site_unreachable", "Accès", 4, 2, "Site injoignable",
                           f"L'audit n'a pas pu ouvrir le site : {sources['audit']['detail']}.",
                           "Un site inaccessible ne peut pas être référencé.",
                           "Vérifier le nom de domaine, l'hébergement et le certificat, puis relancer l'audit."))
        r["rank"] = 1
        return {**base, "health_score": 0, "grade": "E", "counts": {"haute": 1, "moyenne": 0, "basse": 0},
                "quick_wins": [], "recommendations": [r],
                "notes": ["Les diagnostics PageSpeed n'ont pas été utilisés : le site est injoignable."]}

    # Les deux fichiers doivent concerner le même site
    if a_ok and p_ok and audit.get("domain") and perf.get("domain") and audit["domain"] != perf["domain"]:
        notes.append(f"Les résultats PageSpeed concernent {perf['domain']} et l'audit {audit['domain']} : "
                     "PageSpeed est ignoré pour ne pas mélanger deux sites.")
        p_ok = False

    if not a_ok and not p_ok:
        return {**base, "error": "Aucune source exploitable : lancez audit.py puis performance.py sur le site "
                                 f"(audit : {sources['audit']['detail']} ; performance : {sources['performance']['detail']}).",
                "recommendations": []}

    audit_recs, psi_recs = [], []
    if a_ok:
        pages = audit.get("pages") or []
        pages_ok = [p for p in pages if p.get("status") == 200 and "title" in p]
        idx = [p for p in pages_ok if not p.get("noindex")]
        audit_recs += _safe("indexation", _rules_access_and_indexation, notes, audit, pages_ok)
        audit_recs += _safe("sécurité", _rules_security, notes, audit)
        audit_recs += _safe("contenu", _rules_content, notes, idx)
        audit_recs += _safe("liens", _rules_links_and_errors, notes, audit, pages)
        audit_recs += _safe("serveur", _rule_slow_server, notes, audit)
        if len(pages) < 10:
            notes.append(f"Seulement {len(pages)} page(s) explorée(s) : résultats SEO indicatifs. "
                         "Augmenter --max-pages pour un audit plus fiable.")
        summ = audit.get("summary") or {}
        if summ.get("js_rendered_suspected"):
            notes.append("Site probablement en JavaScript : les chiffres de l'audit peuvent être sous-estimés.")
        if summ.get("unreachable_links") or summ.get("blocked_links"):
            notes.append("Certains liens n'ont pas pu être vérifiés (injoignables ou bloquant les robots) : "
                         "non comptés comme cassés.")
    else:
        notes.append(f"Audit SEO indisponible ({sources['audit']['detail']}) : seules les recommandations "
                     "PageSpeed sont fournies et le score de santé n'est pas calculé.")

    if p_ok:
        devs = _usable_devices(perf)
        psi_recs += _safe("performance", _rules_pagespeed_performance, notes, devs)
        psi_recs += _safe("accessibilité", _rules_pagespeed_quality, notes, devs,
                          "accessibility", "accessibility", "Accessibilité", ACC_ACTIONS)
        psi_recs += _safe("bonnes pratiques", _rules_pagespeed_quality, notes, devs,
                          "best_practices", "best-practices", "Bonnes pratiques", BP_ACTIONS)
        notes.append("PageSpeed ne teste que l'URL fournie (en général la page d'accueil) : ses diagnostics "
                     "ne valent pas automatiquement pour tout le site.")
    elif sources["performance"]["status"] != "ok":
        notes.append(f"Performance non évaluée : {sources['performance']['detail']}. "
                     "Aucune estimation n'a été faite.")

    recs = [_finalize(r) for r in _merge(audit_recs, psi_recs)]
    recs.sort(key=lambda r: (PRIORITY_ORDER[r["priority"]], -r["severity"], r["effort_level"], -(r["count"] or 0)))
    for i, r in enumerate(recs, 1):
        r["rank"] = i

    counts = {p: sum(1 for r in recs if r["priority"] == p) for p in ("haute", "moyenne", "basse")}
    if a_ok:
        penalty = sum(PENALTY["blocking"] if r["blocking"] else PENALTY[r["priority"]] for r in recs)
        score = max(0, 100 - penalty)
        grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 40 else "E"
    else:
        score = grade = None
    return {**base, "health_score": score, "grade": grade, "counts": counts,
            "quick_wins": [r["id"] for r in recs if r["quick_win"]],
            "recommendations": recs, "notes": notes}


# =================================================================== LIGNE DE COMMANDE
def _load(path: Path):
    """RÔLE : lire un fichier JSON de results/. SORTIE : (données, état) avec état = 'ok', 'missing' ou 'invalid'
    (fichier absent / JSON corrompu : on ne plante pas, on le signale)."""
    try:
        return json.loads(path.read_text(encoding="utf-8")), "ok"
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError):
        return None, "invalid"


def main() -> None:
    """RÔLE : lancer les recommandations depuis le terminal.
    EXEMPLE : python -m src.recommendations exemple.ma
    Lit results/<domaine>.json (audit) et results/<domaine>_performance.json (PageSpeed) : les DEUX sont facultatifs
    individuellement, mais il en faut au moins un. Écrit results/<domaine>_recommendations.json et affiche le plan
    d'action groupé par priorité."""
    ap = argparse.ArgumentParser(description="Recommandations priorisées (SEO technique + PageSpeed).")
    ap.add_argument("domain", help="domaine audité, ex. exemple.ma (fichiers results/<domaine>*.json)")
    ap.add_argument("--results-dir", default="results", help="dossier des résultats (défaut : results)")
    a = ap.parse_args()
    dom = _host(a.domain).replace(":", "_")
    folder = Path(a.results_dir)
    audit, st_a = _load(folder / f"{dom}.json")
    perf, st_p = _load(folder / f"{dom}_performance.json")
    if st_a == "invalid":
        print(f"⚠ {folder / (dom + '.json')} est illisible (JSON corrompu) : relancez audit.py.")
    if st_p == "invalid":
        print(f"⚠ {folder / (dom + '_performance.json')} est illisible (JSON corrompu) : relancez performance.py.")

    res = generate_recommendations(audit, perf)
    if "error" in res:
        print("Impossible :", res["error"])
        return
    out = folder / f"{dom}_recommendations.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")

    score = f"{res['health_score']}/100 (note {res['grade']})" if res["health_score"] is not None else "non calculé"
    print(f"Score de santé SEO : {score}  |  {res['counts']}")
    for name, s in res["sources"].items():
        print(f"  source {name} : {s['status']}" + (f" ({s['detail']})" if s["status"] != "ok" and s["detail"] else ""))
    if res["quick_wins"]:
        print("Gains rapides :", ", ".join(res["quick_wins"]))
    current = None
    for r in res["recommendations"]:
        if r["priority"] != current:
            current = r["priority"]
            print(f"\n===== PRIORITÉ {current.upper()} =====")
        flag = (" ⚡" if r["quick_win"] else "") + (" ⛔ bloquant" if r["blocking"] else "")
        print(f"\n#{r['rank']} {r['title']}{flag}  [{r['category']} | effort {r['effort']} | {'+'.join(r['sources'])}]")
        print(f"   Problème : {r['problem']}")
        print(f"   À faire  : {r['action']}")
        for ex in r["examples"][:2]:
            print(f"   ex. {ex}")
    for n in res["notes"]:
        print("\nNote :", n)
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()