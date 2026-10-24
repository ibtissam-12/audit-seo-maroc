"""
MODULE A (Personne 1) : MOTEUR D'AUDIT SEO TECHNIQUE
=====================================================

Rôle dans le projet
-------------------
Ce module se comporte comme un petit robot de Google : il visite un site web, lit son HTML
et vérifie les points techniques qui influencent le référencement (SEO) :
accès au site, robots.txt, sitemap, balises title / meta / H1, images sans alt,
liens cassés, HTTPS, vitesse de réponse.

Il ne juge pas le design ni la qualité du texte : seulement ce que Google « lit ».

Place dans l'architecture
-------------------------
    audit.py  (CE FICHIER)  -->  results/<site>.json  -->  performance.py
                                                           recommendations.py
                                                           report.py / app.py

La fonction audit_site(url) est le « contrat » : elle retourne un dictionnaire (compatible JSON)
que les autres modules du groupe lisent. NE PAS changer les noms de clés sans prévenir l'équipe.

Test seul :  python -m src.audit https://www.exemple.ma --max-pages 20
Résultat  :  affiché à l'écran + enregistré dans results/<domaine>.json

Ordre d'exécution de audit_site()
---------------------------------
    1. check_access       le site répond-il ?
    2. check_robots       robots.txt (robots autorisés ? où est le sitemap ?)
    3. check_sitemap      plan du site (sitemap.xml)
    4. crawl              visite des pages  -> analyze_html pour chaque page
    5. find_broken_links  test des liens trouvés
    6. check_https        sécurité HTTPS / certificat
    7. build_summary      chiffres clés (le "summary")
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import time
import xml.etree.ElementTree as ET
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ réglages
UA = "SEOAuditBot/1.0 (projet academique)"   # on s'identifie honnêtement auprès des sites
TIMEOUT = 30                                  # certains sites marocains répondent en plus de 10 s
LINK_TIMEOUT = 20                             # délai maximum pour tester un lien

# Pages "outil" (panier, compte...) sans intérêt SEO : on ne les explore pas
UTILITY_PATHS = ("/cart", "/checkout", "/customer", "/account", "/login", "/signin", "/register",
                 "/wishlist", "/panier", "/compte", "/connexion", "/interaction")
# Fichiers qui ne sont pas des pages HTML : inutile de les analyser
SKIP_EXT = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".css", ".js", ".xml")


# =================================================================== UTILITAIRES
def _session() -> requests.Session:
    """RÔLE : créer la session HTTP utilisée pour toutes les requêtes de l'audit.
    Elle porte notre User-Agent (nom du robot) et réutilise les connexions, ce qui est plus rapide.
    SORTIE : un objet requests.Session."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    return s


def _host(url: str) -> str:
    """RÔLE : extraire le nom de domaine d'une URL, sans le 'www.'.
    Sert à savoir si un lien est INTERNE (même site) ou EXTERNE (autre site).
    EXEMPLE : https://www.exemple.ma/page  ->  'exemple.ma'  (donc exemple.ma == www.exemple.ma)."""
    h = urlparse(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _abs(href: str, base: str):
    """RÔLE : transformer le contenu d'un attribut href en URL absolue propre.
    - convertit un lien relatif ('/contact') en URL complète,
    - supprime le #fragment (ancre),
    - ignore les liens qui ne sont pas des pages web : mailto:, tel:, javascript:, #, data:,
    - ignore les liens /cdn-cgi/ (mécanisme interne de Cloudflare qui masque les e-mails :
      ce n'est pas un vrai lien, il créerait un faux 'lien cassé').
    SORTIE : l'URL absolue (str) ou None si le lien doit être ignoré."""
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return None
    url, _ = urldefrag(urljoin(base, href.strip()))
    if "/cdn-cgi/" in url:
        return None
    return url if urlparse(url).scheme in ("http", "https") else None


# =================================================================== 1. ACCÈS AU SITE
def check_access(url: str, s: requests.Session) -> dict:
    """RÔLE : première vérification, le site répond-il ?
    Ouvre la page d'accueil et note : accessible ou non, code HTTP (200, 404...),
    URL finale après redirections, temps de réponse, et message d'erreur lisible
    (certificat SSL invalide, timeout, domaine introuvable...).
    Si le site est injoignable, audit_site s'arrête proprement (pas de plantage).
    SORTIE : {"reachable", "status", "final_url", "response_time_s", "error"}"""
    res = {"reachable": False, "status": None, "final_url": url, "response_time_s": None, "error": None}
    try:
        r = s.get(url, timeout=TIMEOUT)
        res.update(reachable=r.status_code < 400, status=r.status_code, final_url=r.url,
                   response_time_s=round(r.elapsed.total_seconds(), 2))
        if r.status_code >= 400:
            res["error"] = f"Le site répond avec le code HTTP {r.status_code}"
    except requests.exceptions.SSLError:
        res["error"] = "Erreur de certificat SSL"
    except requests.exceptions.Timeout:
        res["error"] = "Délai dépassé (timeout)"
    except requests.exceptions.ConnectionError:
        res["error"] = "Connexion impossible (domaine introuvable ou serveur éteint)"
    except requests.RequestException as e:
        res["error"] = str(e)[:100]
    return res


# =================================================================== 2. ROBOTS.TXT
def check_robots(base: str, s: requests.Session):
    """RÔLE : lire le fichier /robots.txt, qui dit aux robots ce qu'ils ont le droit de visiter.
    On y relève :
    - s'il existe (un site qui renvoie une page HTML à la place est considéré sans robots.txt),
    - s'il bloque TOUT le site pour tous les robots ('Disallow: /') -> site invisible pour Google,
    - les adresses des sitemaps déclarés.
    On construit aussi un RobotFileParser, utilisé par crawl() pour RESPECTER les interdictions.
    SORTIE : (infos: dict, parseur RobotFileParser ou None si pas de robots.txt)."""
    info = {"exists": False, "status": None, "blocks_all": False, "sitemaps": []}
    try:
        r = s.get(urljoin(base, "/robots.txt"), timeout=TIMEOUT)
    except requests.RequestException:
        return info, None
    info["status"] = r.status_code
    if r.status_code != 200 or "<html" in r.text[:200].lower():
        return info, None
    info["exists"] = True
    for raw in r.text.splitlines():                    # récupère les lignes "Sitemap: ..."
        line = raw.split("#")[0].strip()
        if ":" not in line:
            continue
        key, val = [x.strip() for x in line.split(":", 1)]
        if key.lower() == "sitemap" and val:
            info["sitemaps"].append(val)
    rp = RobotFileParser()
    rp.parse(r.text.splitlines())
    # "Disallow: /" pour tous les robots = site bloqué (gère les groupes multiples et les Allow)
    info["blocks_all"] = not rp.can_fetch("*", urljoin(base, "/"))
    return info, rp


# =================================================================== 3. SITEMAP.XML
def _read_sitemap(url: str, s: requests.Session, depth=0) -> list:
    """RÔLE : lire un fichier sitemap XML et retourner la liste des URLs qu'il contient.
    Gère le cas d'un « index de sitemaps » (sitemap qui pointe vers d'autres sitemaps) :
    on descend dans les 10 premiers, sur 2 niveaux maximum, pour éviter les boucles.
    En cas d'erreur réseau ou de XML invalide : retourne une liste vide (pas de plantage).
    SORTIE : liste d'URLs (str)."""
    try:
        r = s.get(url, timeout=TIMEOUT)
        root = ET.fromstring(r.content) if r.status_code == 200 else None
    except (requests.RequestException, ET.ParseError):
        return []
    if root is None or depth > 2:
        return []
    locs = [e.text.strip() for e in root.iter() if e.tag.endswith("loc") and e.text]
    if root.tag.endswith("sitemapindex"):             # index de sitemaps -> on descend
        out = []
        for child in locs[:10]:
            out += _read_sitemap(child, s, depth + 1)
        return out
    return locs


def check_sitemap(base: str, robots: dict, s: requests.Session):
    """RÔLE : trouver et lire le sitemap du site (le « plan du site » donné à Google).
    On cherche d'abord les adresses déclarées dans robots.txt ; à défaut on essaie
    /sitemap.xml puis /sitemap_index.xml.
    Les URLs trouvées servent aussi au crawl pour découvrir des pages non liées depuis l'accueil.
    SORTIE : (infos: {"exists", "url", "url_count", "in_robots"}, liste d'URLs)."""
    candidates = robots["sitemaps"] or [urljoin(base, "/sitemap.xml"), urljoin(base, "/sitemap_index.xml")]
    for c in candidates:
        urls = _read_sitemap(c, s)
        if urls:
            return {"exists": True, "url": c, "url_count": len(urls), "in_robots": bool(robots["sitemaps"])}, urls
    return {"exists": False, "url": None, "url_count": 0, "in_robots": bool(robots["sitemaps"])}, []


# =================================================================== 4. ANALYSE D'UNE PAGE
def analyze_html(html, url: str) -> dict:
    """RÔLE : analyser UNE page HTML et en extraire les éléments SEO.
    ENTRÉE : le HTML (texte ou octets : avec des octets, BeautifulSoup détecte lui-même
    l'encodage, ce qui évite les accents cassés) et l'URL de la page.
    EXTRAIT : title (+ longueur), meta description (+ longueur), H1 (nombre + texte),
    images (total et sans attribut alt ; alt="" est accepté car décoratif), canonical,
    balise viewport (site adapté au mobile), noindex, nombre de mots, et liste des liens.
    La clé "_links" est interne : elle sert au crawl puis est retirée du résultat final.
    SORTIE : dictionnaire des mesures de la page."""
    soup = BeautifulSoup(html, "lxml")
    title = soup.title.get_text(strip=True) if soup.title else None
    meta = soup.find("meta", attrs={"name": "description"})
    meta_desc = (meta.get("content") or "").strip() if meta else None
    robots_meta = soup.find("meta", attrs={"name": "robots"})
    imgs = soup.find_all("img")
    canonical = soup.find("link", rel="canonical")
    links = []
    for a in soup.find_all("a", href=True):
        link = _abs(a["href"], url)
        if link:
            links.append(link)
    return {
        "title": title, "title_len": len(title or ""),
        "meta_desc": meta_desc, "meta_len": len(meta_desc or ""),
        "h1_count": len(soup.find_all("h1")),
        "h1": [h.get_text(" ", strip=True) for h in soup.find_all("h1")][:3],
        "images_total": len(imgs),
        "images_missing_alt": sum(1 for i in imgs if i.get("alt") is None),  # alt="" = décoratif, accepté
        "canonical": canonical.get("href") if canonical else None,
        "viewport": soup.find("meta", attrs={"name": "viewport"}) is not None,
        "noindex": "noindex" in ((robots_meta.get("content") or "").lower() if robots_meta else ""),
        "word_count": len(soup.get_text(" ", strip=True).split()),
        "_links": list(dict.fromkeys(links)),          # liens sans doublons, ordre conservé
    }


# =================================================================== 5. CRAWL
def crawl(start: str, s: requests.Session, rp, sitemap_urls, max_pages=30, delay=0.4) -> list:
    """RÔLE : parcourir le site page par page (parcours en largeur, comme un robot de Google).
    FONCTIONNEMENT :
    1. on part de la page d'accueil et on suit les liens INTERNES trouvés ;
    2. quand il n'y a plus de liens à suivre, on complète avec un échantillon réparti du sitemap
       (1 URL sur N) pour ne pas ne voir qu'une seule catégorie de pages ;
    3. on s'arrête à max_pages.
    RÈGLES DE POLITESSE ET DE QUALITÉ :
    - respecte robots.txt ;
    - pause (delay) entre deux requêtes pour ne pas surcharger le site ;
    - ignore les pages 'outil' (panier, compte...), les fichiers non-HTML et les liens externes ;
    - une page atteinte par deux URLs (redirection) n'est comptée qu'une seule fois ;
    - une redirection vers un autre domaine est ignorée.
    SORTIE : liste de dictionnaires, un par page (statut, redirections, temps, taille + analyse HTML)."""
    host = _host(start)
    step = max(1, len(sitemap_urls) // max(max_pages, 1))
    pending = sitemap_urls[::step][: max_pages * 2]
    queue, seen, pages = deque([start]), set(), []
    while (queue or pending) and len(pages) < max_pages:
        if not queue:
            queue.extend(pending)
            pending = []
        url = queue.popleft()
        path = urlparse(url).path.lower()
        if (url in seen or path.endswith(SKIP_EXT) or _host(url) != host
                or any(k in path for k in UTILITY_PATHS)):
            continue
        seen.add(url)
        if rp and url != start and not rp.can_fetch(UA, url):
            continue                                   # on respecte robots.txt
        try:
            r = s.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            pages.append({"url": url, "status": None, "error": str(e)[:80]})
            continue
        if _host(r.url) != host:
            continue                                   # redirigée hors du site : ignorée
        if r.url != url:                               # redirection : évite de compter la même page deux fois
            if r.url in seen:
                continue
            seen.add(r.url)
        page = {"url": r.url, "status": r.status_code, "redirects": len(r.history),
                "ttfb_s": round(r.elapsed.total_seconds(), 2), "size_kb": round(len(r.content) / 1024, 1)}
        if r.status_code == 200 and "html" in r.headers.get("Content-Type", ""):
            page.update(analyze_html(r.content, r.url))   # octets : BeautifulSoup détecte l'encodage
            queue.extend(l for l in page["_links"] if l not in seen)
        pages.append(page)
        time.sleep(delay)                              # crawl poli
    return pages


# =================================================================== 6. LIENS CASSÉS
def _status(url: str, s: requests.Session):
    """RÔLE : obtenir le code HTTP d'un lien, le plus légèrement possible.
    On essaie d'abord HEAD (sans télécharger la page) ; si le serveur refuse HEAD
    (403, 405, 501, erreur 5xx), on retente en GET. Deux essais au total, car un site lent
    peut expirer une première fois.
    SORTIE : (url, code HTTP) ; code = None si le lien est injoignable (timeout, DNS...)."""
    for attempt in range(2):
        for method in (s.head, s.get):
            try:
                r = method(url, timeout=LINK_TIMEOUT, allow_redirects=True,
                           **({"stream": True} if method == s.get else {}))
                if method == s.head and (r.status_code in (403, 405, 501) or r.status_code >= 500):
                    continue                           # certains serveurs refusent HEAD -> on tente GET
                r.close()
                return url, r.status_code
            except requests.RequestException:
                continue
        time.sleep(1)
    return url, None


def _classify(status, internal: bool) -> str:
    """RÔLE : donner une catégorie honnête à un lien selon son code HTTP.
    - 'ok'          : fonctionne ;
    - 'broken'      : VRAI lien cassé (404, 410, erreurs 5xx...) ;
    - 'blocked'     : 401/403/429/999, le site bloque les robots (le lien existe sans doute) ;
    - 'unreachable' : aucune réponse (timeout, domaine introuvable) : cassé NON prouvé.
    Cette distinction évite d'accuser un site à tort."""
    if status is None:
        return "unreachable"
    if status in (401, 403, 429, 999):
        return "blocked"
    return "broken" if status >= 400 else "ok"


def find_broken_links(pages: list, host: str, s: requests.Session, max_links=150) -> list:
    """RÔLE : tester les liens trouvés pendant le crawl et lister ceux qui posent problème.
    - réutilise le statut des pages déjà visitées (pas de requête en double) ;
    - teste les autres liens en parallèle (5 threads seulement, pour ménager le site),
      dans la limite de max_links liens ;
    - note sur quelle page chaque lien a été trouvé (found_on) et s'il est interne ou externe.
    SORTIE : liste triée (cassés d'abord, puis injoignables, puis bloqués) de
    {"url", "status", "category", "internal", "found_on"}. Les liens 'ok' ne sont pas listés."""
    found_on = {}
    for p in pages:
        for l in p.get("_links", []):
            found_on.setdefault(l, p["url"])
    known = {p["url"]: p["status"] for p in pages if p.get("status")}   # statuts déjà connus
    todo = [u for u in found_on if u not in known][:max_links]
    with ThreadPoolExecutor(max_workers=5) as ex:
        known.update(dict(ex.map(lambda u: _status(u, s), todo)))
    result = []
    for url, st in known.items():
        if url not in found_on:
            continue
        internal = _host(url) == host
        cat = _classify(st, internal)
        if cat != "ok":
            result.append({"url": url, "status": st, "category": cat, "internal": internal, "found_on": found_on[url]})
    order = {"broken": 0, "unreachable": 1, "blocked": 2}
    result.sort(key=lambda b: (order[b["category"]], not b["internal"]))
    return result


# =================================================================== 7. HTTPS
def check_https(url: str, s: requests.Session) -> dict:
    """RÔLE : vérifier la sécurité de la connexion (le cadenas du navigateur).
    Contrôle : HTTPS disponible ? la version http redirige-t-elle vers https ?
    l'en-tête HSTS (force HTTPS) est-il présent ? le certificat est-il valide et dans
    combien de jours expire-t-il ?
    SORTIE : {"https_available", "http_redirects_to_https", "hsts", "cert_valid", "cert_days_left"}."""
    netloc = urlparse(url).netloc
    res = {"https_available": False, "http_redirects_to_https": None, "hsts": False,
           "cert_valid": None, "cert_days_left": None}
    try:
        r = s.get(f"https://{netloc}/", timeout=TIMEOUT)
        res["https_available"] = True
        res["hsts"] = "strict-transport-security" in {k.lower() for k in r.headers}
    except requests.exceptions.SSLError:
        res["cert_valid"] = False
    except requests.RequestException:
        pass
    try:
        res["http_redirects_to_https"] = s.get(f"http://{netloc}/", timeout=TIMEOUT).url.startswith("https://")
    except requests.RequestException:
        pass
    if res["https_available"] and ":" not in netloc:   # date d'expiration du certificat
        try:
            with socket.create_connection((netloc, 443), timeout=8) as sock:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=netloc) as tls:
                    exp = datetime.strptime(tls.getpeercert()["notAfter"], "%b %d %H:%M:%S %Y %Z")
            res["cert_valid"] = True
            res["cert_days_left"] = (exp.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).days
        except ssl.SSLCertVerificationError:
            res["cert_valid"] = False
        except OSError:
            pass
    return res


# =================================================================== 8. RÉSUMÉ CHIFFRÉ
def build_summary(pages: list, broken: list) -> dict:
    """RÔLE : transformer les mesures page par page en CHIFFRES CLÉS pour comparer les sites.
    C'est le bloc 'summary' que lisent recommendations.py, report.py et app.py.
    RÈGLE IMPORTANTE : les contrôles de contenu (title, meta, H1, doublons) ne portent que sur
    les pages INDEXABLES. Une page 'noindex' (aide, panier...) n'est pas destinée à Google :
    elle est comptée à part ('noindex_pages') et ne fausse pas les résultats.
    SEUILS UTILISÉS : title > 60 car. trop long, < 30 trop court ; meta > 160 trop longue,
    < 70 trop courte ; page lente si temps de réponse > 1,5 s.
    EN PLUS : 'js_rendered_suspected' vaut True si la page d'accueil est presque vide
    (moins de 80 mots, moins de 5 liens) : site probablement en JavaScript, résultats peu fiables.
    SORTIE : dictionnaire de compteurs (0 = idéal pour presque tous)."""
    ok = [p for p in pages if p.get("status") == 200 and "title" in p]
    idx = [p for p in ok if not p["noindex"]]
    titles = Counter(p["title"] for p in idx if p["title"])
    metas = Counter(p["meta_desc"] for p in idx if p["meta_desc"])
    first = ok[0] if ok else None
    wc = first.get("word_count") if first else None
    js_suspected = wc is not None and wc < 80 and len(first.get("_links", [])) < 5
    return {
        "js_rendered_suspected": js_suspected,
        "pages_crawled": len(pages), "pages_html_ok": len(ok), "pages_indexable": len(idx),
        "noindex_pages": len(ok) - len(idx),
        "pages_error": sum(1 for p in pages if p.get("status") is None or p["status"] >= 400),
        "missing_title": sum(1 for p in idx if not p["title"]),
        "duplicate_title": sum(1 for p in idx if p["title"] and titles[p["title"]] > 1),
        "title_too_long": sum(1 for p in idx if p["title_len"] > 60),
        "title_too_short": sum(1 for p in idx if 0 < p["title_len"] < 30),
        "missing_meta": sum(1 for p in idx if not p["meta_desc"]),
        "duplicate_meta": sum(1 for p in idx if p["meta_desc"] and metas[p["meta_desc"]] > 1),
        "meta_too_long": sum(1 for p in idx if p["meta_len"] > 160),
        "meta_too_short": sum(1 for p in idx if 0 < p["meta_len"] < 70),
        "missing_h1": sum(1 for p in idx if p["h1_count"] == 0),
        "multiple_h1": sum(1 for p in idx if p["h1_count"] > 1),
        "images_missing_alt": sum(p["images_missing_alt"] for p in idx),
        "broken_links": sum(1 for b in broken if b["category"] == "broken"),
        "unreachable_links": sum(1 for b in broken if b["category"] == "unreachable"),
        "blocked_links": sum(1 for b in broken if b["category"] == "blocked"),
        "slow_pages": sum(1 for p in ok if p.get("ttfb_s", 0) > 1.5),
        "avg_ttfb_s": round(sum(p.get("ttfb_s", 0) for p in ok) / len(ok), 2) if ok else None,
    }


# =================================================================== 9. FONCTION PRINCIPALE
def audit_site(url: str, max_pages: int = 30, delay: float = 0.4) -> dict:
    """RÔLE : FONCTION PRINCIPALE du module, celle qu'appellent les autres modules et app.py.
    Lance toutes les vérifications dans l'ordre (accès, robots, sitemap, crawl, liens, HTTPS,
    résumé) et assemble le résultat.
    PARAMÈTRES : url (avec ou sans https://), max_pages (nombre de pages à analyser),
    delay (pause en secondes entre deux pages).
    SI LE SITE EST INJOIGNABLE : retourne quand même un dictionnaire avec la clé "error"
    (et sans "summary"), sans planter ; les modules suivants doivent tester la présence de "error".
    SORTIE (le « contrat ») : url, domain, audited_at, access, robots, sitemap, https,
    pages, broken_links, summary. 100 % compatible JSON."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    s = _session()
    access = check_access(url, s)
    if not access["reachable"]:                        # site injoignable : on ne plante pas
        return {"url": url, "domain": _host(url), "audited_at": datetime.now().isoformat(timespec="seconds"),
                "access": access, "error": access["error"]}
    start = access["final_url"]                        # URL finale après redirections
    base = f"{urlparse(start).scheme}://{urlparse(start).netloc}"

    robots, rp = check_robots(base, s)
    sitemap, sitemap_urls = check_sitemap(base, robots, s)
    pages = crawl(start, s, rp, sitemap_urls, max_pages, delay)
    broken = find_broken_links(pages, _host(start), s)
    https = check_https(url, s)

    summary = build_summary(pages, broken)
    for p in pages:                                    # retire les listes internes (lourdes)
        p.pop("_links", None)
    return {"url": start, "domain": _host(start), "audited_at": datetime.now().isoformat(timespec="seconds"),
            "access": access, "robots": robots, "sitemap": sitemap, "https": https,
            "pages": pages, "broken_links": broken, "summary": summary}


# =================================================================== LIGNE DE COMMANDE
def main() -> None:
    """RÔLE : permettre de lancer l'audit depuis le terminal et d'enregistrer le résultat.
    EXEMPLE : python -m src.audit https://www.exemple.ma --max-pages 20
    Écrit results/<domaine>.json (lu par les autres modules) et affiche le résumé.
    Si le site est injoignable, affiche la raison au lieu de planter."""
    ap = argparse.ArgumentParser(description="Audit SEO technique d'un site web.")
    ap.add_argument("url", help="adresse du site, ex. https://www.exemple.ma")
    ap.add_argument("--max-pages", type=int, default=30, help="nombre de pages à analyser (défaut 30)")
    a = ap.parse_args()
    result = audit_site(a.url, a.max_pages)
    out = Path("results") / f"{result['domain'].replace(':', '_')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if "summary" in result:
        print(json.dumps(result["summary"], indent=2), f"\n-> {out}")
    else:                                              # site injoignable : pas de résumé
        print("Audit impossible :", result.get("error"), f"\n-> {out}")


if __name__ == "__main__":
    main()