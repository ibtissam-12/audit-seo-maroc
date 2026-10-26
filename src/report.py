"""
report.py : lecture des résultats de l'équipe + génération du rapport PDF.

Ce fichier contient deux choses :
  1. L'ADAPTATEUR DE DONNÉES (load_sites, build_issues, cwv_items, comparison_rows...) : le seul endroit qui
     connaît le format des JSON de l'équipe :
         audit.py            -> results/<domaine>.json
         performance.py      -> results/<domaine>_performance.json
         recommendations.py  -> calculé à la volée par generate_recommendations(audit, perf)
     Une « fiche site » est un dict : {"domain", "audit", "perf" (ou None), "reco"}.
  2. LE RAPPORT PDF (generate_pdf) : synthèse, comparaison, performance, détail par site, plan d'action.

Usage en ligne de commande :  python -m src.report [dossier_results] [fichier_sortie.pdf]
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import matplotlib

matplotlib.use("Agg")  # backend sans fenêtre (nécessaire avec Streamlit)
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas
from reportlab.platypus import (CondPageBreak, HRFlowable, Image, KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

try:
    from src.recommendations import generate_recommendations
except ImportError:  # branche recommandations pas encore fusionnée
    generate_recommendations = None

# --------------------------------------------------------------------------- #
# Lecture des résultats
# --------------------------------------------------------------------------- #
def _read(path: Path):
    """Lit un JSON ; None si le fichier est absent ou corrompu (jamais d'exception)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_sites(folder: str | Path = "results") -> list[dict]:
    """Charge tous les sites du dossier results/ (un audit <domaine>.json = un site)."""
    folder, sites = Path(folder), []
    for path in sorted(folder.glob("*.json")):
        if path.stem.endswith(("_performance", "_recommendations")):
            continue
        audit = _read(path)
        if not isinstance(audit, dict):
            continue
        perf = _read(folder / f"{path.stem}_performance.json")
        if generate_recommendations:
            reco = generate_recommendations(audit, perf)
        else:
            reco = {"recommendations": [], "counts": {}, "notes": [], "sources": {},
                    "error": "src/recommendations.py introuvable"}
        sites.append({"domain": audit.get("domain", path.stem), "audit": audit, "perf": perf, "reco": reco})
    return sites


# --------------------------------------------------------------------------- #
# Petits utilitaires
# --------------------------------------------------------------------------- #
def fr(x, nd: int = 1) -> str:
    """Nombre à la française : 4.8 -> '4,8' ; None -> 'N/D'."""
    if x is None:
        return "N/D"
    s = f"{x:.{nd}f}".rstrip("0").rstrip(".") if nd else str(round(x))
    return s.replace(".", ",")


def summary_of(site: dict) -> dict:
    return site["audit"].get("summary") or {}


def device(site: dict, name: str) -> dict | None:
    """Bloc PageSpeed d'un appareil ('mobile' / 'desktop') s'il est exploitable, sinon None."""
    block = (site.get("perf") or {}).get(name)
    return block if isinstance(block, dict) and block.get("available") else None


def https_ok(site: dict) -> bool | None:
    h = site["audit"].get("https")
    if not h:
        return None
    return bool(h.get("https_available")) and h.get("cert_valid") is not False


# --------------------------------------------------------------------------- #
# Core Web Vitals et mesures PageSpeed
# --------------------------------------------------------------------------- #
RATING_COLOR = {"bon": "green", "à améliorer": "orange", "mauvais": "red", None: "gray"}
FIELD_RATING = {"FAST": "bon", "AVERAGE": "à améliorer", "SLOW": "mauvais"}


def cwv_items(block: dict | None) -> list[dict]:
    """
    LCP, INP, CLS, TBT d'un appareil. L'INP n'existe qu'en données terrain (vrais visiteurs) ;
    le TBT est son équivalent de laboratoire.
    Chaque élément : {name, value, rating, threshold, raw, note}.
    """
    block = block or {}
    lab, rt = block.get("lab") or {}, block.get("lab_rating") or {}
    inp = (block.get("field") or {}).get("inp_ms") or {}
    return [
        {"name": "LCP", "raw": lab.get("lcp_s"), "value": fr(lab.get("lcp_s"), 2) + (" s" if lab.get("lcp_s") is not None else ""),
         "rating": rt.get("lcp_s"), "threshold": "≤ 2,5 s", "note": "Chargement du contenu principal"},
        {"name": "INP", "raw": inp.get("value"), "value": fr(inp.get("value"), 0) + (" ms" if inp.get("value") is not None else ""),
         "rating": FIELD_RATING.get(inp.get("category")), "threshold": "≤ 200 ms",
         "note": "Réactivité (données terrain)"},
        {"name": "CLS", "raw": lab.get("cls"), "value": fr(lab.get("cls"), 3), "rating": rt.get("cls"),
         "threshold": "≤ 0,1", "note": "Stabilité visuelle"},
        {"name": "TBT", "raw": lab.get("tbt_ms"), "value": fr(lab.get("tbt_ms"), 0) + (" ms" if lab.get("tbt_ms") is not None else ""),
         "rating": rt.get("tbt_ms"), "threshold": "≤ 200 ms", "note": "Blocage JavaScript (laboratoire)"},
    ]


# --------------------------------------------------------------------------- #
# Indicateurs SEO techniques (bloc "summary" de audit.py)
# --------------------------------------------------------------------------- #
INDICATORS = [
    ("pages_error", "Pages en erreur"), ("missing_title", "Pages sans title"),
    ("duplicate_title", "Titles en double"), ("title_too_long", "Titles trop longs (> 60 car.)"),
    ("title_too_short", "Titles trop courts (< 30 car.)"), ("missing_meta", "Pages sans meta description"),
    ("duplicate_meta", "Meta descriptions en double"), ("meta_too_long", "Meta trop longues (> 160 car.)"),
    ("meta_too_short", "Meta trop courtes (< 70 car.)"), ("missing_h1", "Pages sans H1"),
    ("multiple_h1", "Pages avec plusieurs H1"), ("images_missing_alt", "Images sans attribut alt"),
    ("broken_links", "Liens cassés"), ("unreachable_links", "Liens injoignables (non prouvés cassés)"),
    ("blocked_links", "Liens bloquant les robots"), ("slow_pages", "Pages lentes (> 1,5 s)"),
]


def indicator_rows(site: dict) -> list[dict]:
    """Une ligne par indicateur : à corriger d'abord, puis OK."""
    s = summary_of(site)
    rows = [{"Indicateur": label, "Valeur": s.get(key, 0), "Statut": "À corriger" if s.get(key, 0) else "OK"}
            for key, label in INDICATORS if key in s]
    if "noindex_pages" in s:
        rows.append({"Indicateur": "Pages en noindex (information)", "Valeur": s["noindex_pages"], "Statut": "Info"})
    return sorted(rows, key=lambda r: (r["Statut"] != "À corriger", -r["Valeur"]))


# --------------------------------------------------------------------------- #
# Tableau comparatif (utilisé par l'app ET le rapport PDF)
# --------------------------------------------------------------------------- #
def _yes(v) -> str:
    return "N/D" if v is None else "Oui" if v else "Non"


def comparison_rows(sites: list[dict]) -> list[tuple[str, list[str]]]:
    """[(critère, [valeur pour chaque site])] : toutes les valeurs sont déjà des textes."""
    def row(label, fn):
        out = []
        for s in sites:
            try:
                out.append(str(fn(s)))
            except (KeyError, TypeError, AttributeError):
                out.append("N/D")
        return label, out

    def score(s):
        r = s["reco"]
        return f"{r['health_score']}/100 ({r['grade']})" if r.get("health_score") is not None else "N/D"

    def mob(key, nd=1, unit=""):
        def f(s):
            b = device(s, "mobile")
            v = (b.get("lab") or {}).get(key) if b else None
            return fr(v, nd) + (unit if v is not None else "")
        return f

    def cat(block, key):
        def f(s):
            b = device(s, block)
            q = (b.get(key) or {}) if b else {}
            return q.get("score", "N/D") if q else "N/D"
        return f

    def sm(*keys):
        return lambda s: sum(summary_of(s).get(k, 0) for k in keys)

    return [
        row("Score de santé SEO", score),
        row("Pages explorées", lambda s: summary_of(s)["pages_crawled"]),
        row("HTTPS valide", lambda s: _yes(https_ok(s))),
        row("Redirection http vers https", lambda s: _yes(s["audit"]["https"]["http_redirects_to_https"])),
        row("robots.txt présent", lambda s: _yes(s["audit"]["robots"]["exists"])),
        row("Sitemap présent", lambda s: _yes(s["audit"]["sitemap"]["exists"])),
        row("Temps de réponse moyen", lambda s: fr(summary_of(s)["avg_ttfb_s"], 2) + " s"),
        row("Problèmes de title", sm("missing_title", "duplicate_title", "title_too_long", "title_too_short")),
        row("Problèmes de meta description", sm("missing_meta", "duplicate_meta", "meta_too_long", "meta_too_short")),
        row("Pages sans H1 / à H1 multiples", lambda s: f"{summary_of(s)['missing_h1']} / {summary_of(s)['multiple_h1']}"),
        row("Images sans alt", sm("images_missing_alt")),
        row("Liens cassés", sm("broken_links")),
        row("Recommandations haute priorité", lambda s: s["reco"]["counts"]["haute"]),
        row("Performance mobile (/100)", lambda s: device(s, "mobile")["score"]),
        row("Performance desktop (/100)", lambda s: device(s, "desktop")["score"]),
        row("LCP mobile", mob("lcp_s", 2, " s")),
        row("CLS mobile", mob("cls", 3)),
        row("TBT mobile", mob("tbt_ms", 0, " ms")),
        row("Accessibilité mobile (/100)", cat("mobile", "accessibility")),
        row("Bonnes pratiques mobile (/100)", cat("mobile", "best_practices")),
    ]

# --------------------------------------------------------------------------- #
# Problèmes détectés, URL par URL (onglets « Constats » du dashboard)
# --------------------------------------------------------------------------- #
CATEGORIES = {"tags": "Balises SEO", "crawl": "Exploration & indexation", "images": "Images", "links": "Liens & sécurité"}
HIGH, MED, LOW = "Haute", "Moyenne", "Faible"
SEV_ORDER = {HIGH: 0, MED: 1, LOW: 2}


def short_url(url: str) -> str:
    """https://www.site.ma/page?x=1 -> /page?x=1 (lien interne) ; lien externe : sans le https://."""
    p = urlparse(url)
    return (p.path or "/") + (f"?{p.query}" if p.query else "")


def build_issues(site: dict) -> list[dict]:
    """
    Liste des problèmes d'un site : [{category, issue, url, severity}], triés par gravité.
    Calculée à partir des pages explorées par audit.py (mêmes seuils que audit.py :
    title 30-60 car., meta 70-160 car.) ; seules les pages indexables sont contrôlées pour le contenu.
    """
    a = site["audit"]
    pages = a.get("pages") or []
    ok = [p for p in pages if p.get("status") == 200 and "title" in p]
    idx = [p for p in ok if not p.get("noindex")]
    titles = Counter(p["title"] for p in idx if p.get("title"))
    metas = Counter(p["meta_desc"] for p in idx if p.get("meta_desc"))
    out: list[dict] = []

    def add(cat, issue, url, sev):
        out.append({"category": cat, "issue": issue, "url": url, "severity": sev})

    for p in idx:
        u = short_url(p["url"])
        if not p.get("title"):
            add("tags", "Title manquant", u, HIGH)
        else:
            if titles[p["title"]] > 1:
                add("tags", "Title en double", u, MED)
            if p.get("title_len", 0) > 60:
                add("tags", "Title trop long (> 60 car.)", u, LOW)
            elif p.get("title_len", 0) < 30:
                add("tags", "Title trop court (< 30 car.)", u, LOW)
        if not p.get("meta_desc"):
            add("tags", "Meta description manquante", u, HIGH)
        else:
            if metas[p["meta_desc"]] > 1:
                add("tags", "Meta description en double", u, MED)
            if p.get("meta_len", 0) > 160:
                add("tags", "Meta description trop longue (> 160 car.)", u, LOW)
            elif p.get("meta_len", 0) < 70:
                add("tags", "Meta description trop courte (< 70 car.)", u, LOW)
        if p.get("h1_count", 1) == 0:
            add("tags", "H1 manquant", u, MED)
        elif p.get("h1_count", 1) > 1:
            add("tags", "Plusieurs balises H1", u, LOW)
        n = p.get("images_missing_alt", 0)
        if n:
            add("images", f"{n} image(s) sans attribut alt", u, MED if n >= 10 else LOW)

    for p in pages:
        u, st_ = short_url(p.get("url", "")), p.get("status")
        if st_ is None or st_ >= 400:
            add("crawl", f"Page en erreur ({'sans réponse' if st_ is None else 'HTTP ' + str(st_)})", u, HIGH)
        elif p.get("redirects", 0) >= 2:
            add("crawl", f"Chaîne de {p['redirects']} redirections", u, MED)
        elif p.get("ttfb_s", 0) > 1.5:
            add("crawl", f"Réponse serveur lente ({fr(p['ttfb_s'], 1)} s)", u, MED if p["ttfb_s"] > 3 else LOW)
        if p.get("noindex"):
            add("crawl", "Page en noindex (à vérifier)", u, LOW)

    rb, sm, summ = a.get("robots") or {}, a.get("sitemap") or {}, a.get("summary") or {}
    if rb.get("blocks_all"):
        add("crawl", "Site entièrement bloqué par robots.txt", "/robots.txt", HIGH)
    elif rb and rb.get("exists") is False:
        add("crawl", "robots.txt absent", "/robots.txt", LOW)
    if sm and sm.get("exists") is False:
        add("crawl", "sitemap.xml introuvable", "/sitemap.xml", MED)
    elif sm.get("exists") and not sm.get("in_robots"):
        add("crawl", "Sitemap non déclaré dans robots.txt", "/robots.txt", LOW)
    if summ.get("js_rendered_suspected"):
        add("crawl", "Site probablement généré en JavaScript", "/", MED)

    h = a.get("https") or {}
    if h:
        if h.get("https_available") is False:
            add("links", "HTTPS indisponible ou certificat refusé", "/", HIGH)
        elif h.get("cert_valid") is False:
            add("links", "Certificat SSL invalide", "/", HIGH)
        days = h.get("cert_days_left")
        if days is not None and days < 30 and h.get("cert_valid") is not False:
            add("links", f"Certificat SSL : expire dans {days} jour(s)", "/", HIGH if days <= 7 else MED)
        if h.get("http_redirects_to_https") is False:
            add("links", "http:// ne redirige pas vers https://", "/", MED)
        if h.get("hsts") is False and h.get("https_available"):
            add("links", "En-tête HSTS absent", "/", LOW)
    for b in a.get("broken_links") or []:
        target = short_url(b["url"]) if b.get("internal") else b["url"].split("//")[-1]
        kind = {"broken": ("Lien cassé", HIGH), "unreachable": ("Lien injoignable", MED),
                "blocked": ("Lien bloquant les robots", LOW)}.get(b.get("category"), ("Lien à vérifier", LOW))
        add("links", f"{kind[0]} {'interne' if b.get('internal') else 'externe'} (code {b.get('status') or 'N/D'})",
            target, kind[1] if b.get("internal") or b.get("category") != "broken" else MED)
    return sorted(out, key=lambda i: SEV_ORDER[i["severity"]])


# --------------------------------------------------------------------------- #
# RAPPORT PDF : mise en page « rapport d'audit » (couverture, synthèse, comparatif, performance,
# une section par site, chantiers prioritaires, méthode). Police standard Helvetica (aucune installation).
# --------------------------------------------------------------------------- #
AUTHORS = ""   # ex. "Asmae · Kaoutar · Votre nom" : affiché sur la page de garde si renseigné
COURSE = "Module Webmarketing et CRM · TDIA3 · ENSAH · 2026/2027"
HEADER_RIGHT = [""]   # texte à droite de l'en-tête (défini à chaque génération)

NAVY, BLUE = colors.HexColor("#0B2545"), colors.HexColor("#1D4ED8")
INK, GREY = colors.HexColor("#1B2430"), colors.HexColor("#5B6B7F")
LINE, PALE = colors.HexColor("#D5DBE3"), colors.HexColor("#F4F6F9")
GOOD, WARN, BAD = "#1F9D6B", "#D98E04", "#C0392B"
CHIP = {"haute": ("#B42318", "#FEE4E2"), "moyenne": ("#B54708", "#FEF0C7"),
        "basse": ("#067647", "#D1FADF"), "faible": ("#067647", "#D1FADF")}
RATING_BG = {"bon": "#D1FADF", "à améliorer": "#FEF0C7", "mauvais": "#FEE4E2", None: "#ECEFF3"}
MAX_RECO_PER_SITE = 10
W = A4[0] - 4 * cm  # largeur utile

# Helvetica (police standard du PDF) ne contient pas ces caractères : on les remplace.
_REPL = {"≤": "<=", "≥": ">=", "→": "->", "\u202f": " ", "\u00a0": " ", "⚡": "", "⛔": "", "–": "-", "…": "..."}


def plain(text) -> str:
    """Texte sûr pour la police du PDF (caractères absents remplacés)."""
    s = str(text)
    for a, b in _REPL.items():
        s = s.replace(a, b)
    return s.encode("cp1252", "replace").decode("cp1252")


def clean(text) -> str:
    return escape(plain(text))


def band(score) -> str:
    return GOOD if score is not None and score >= 75 else WARN if score is not None and score >= 50 else BAD if score is not None else "#9AA5B1"


def _styles() -> dict:
    b = getSampleStyleSheet()["BodyText"]
    mk = lambda n, **k: ParagraphStyle(n, parent=b, fontName=k.pop("fontName", "Helvetica"), **k)
    return {
        "h1": mk("h1", fontName="Helvetica-Bold", fontSize=20, leading=24, textColor=NAVY, spaceAfter=2),
        "h2": mk("h2", fontName="Helvetica-Bold", fontSize=12.5, leading=16, textColor=NAVY, spaceBefore=12, spaceAfter=5),
        "h3": mk("h3", fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=BLUE, spaceAfter=3),
        "body": mk("body", fontSize=9.6, leading=14, textColor=INK),
        "bul": mk("bul", fontSize=9.2, leading=12.6, textColor=INK, leftIndent=11, bulletIndent=1, spaceAfter=1),
        "cell": mk("cell", fontSize=8.3, leading=10.8, textColor=INK),
        "cellb": mk("cellb", fontName="Helvetica-Bold", fontSize=8.3, leading=10.8, textColor=INK),
        "head": mk("head", fontName="Helvetica-Bold", fontSize=8.2, leading=10.5, textColor=colors.white),
        "small": mk("small", fontSize=7.8, leading=10, textColor=GREY),
        "lab": mk("lab", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=GREY),
        "sub": mk("sub", fontSize=9.5, leading=13, textColor=GREY, spaceAfter=8),
    }


ST = _styles()


def P(text, style="cell", **kw) -> Paragraph:
    st = ST[style]
    if kw:
        st = ParagraphStyle("x", parent=st, **kw)
    return Paragraph(clean(text), st)


def bullets(items, style="bul"):
    return [Paragraph(clean(t), ST[style], bulletText="•") for t in items]


def title(num: int, text: str, sub: str = "") -> list:
    out = [Paragraph(f'<font color="#1D4ED8">{num}.</font>&nbsp;{clean(text)}', ST["h1"]),
           HRFlowable(width="100%", thickness=1.4, color=BLUE, spaceBefore=3, spaceAfter=8)]
    return out + ([Paragraph(clean(sub), ST["sub"])] if sub else [])


def tbl(rows, widths, extra=None, header=True) -> Table:
    """Tableau épuré : en-tête bleu marine, filets horizontaux fins, pas de quadrillage."""
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("TOPPADDING", (0, 0), (-1, -1), 4.5),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5), ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
             ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE)]
    if header:
        style += [("BACKGROUND", (0, 0), (-1, 0), NAVY), ("LINEBELOW", (0, 0), (-1, 0), 0, NAVY)]
    t.setStyle(TableStyle(style + (extra or [])))
    return t


def chip_cmd(col: int, row: int, level: str) -> list:
    fg, bg = CHIP.get(level.lower(), ("#344054", "#ECEFF3"))
    return [("BACKGROUND", (col, row), (col, row), colors.HexColor(bg))]


def chip(level: str) -> Paragraph:
    fg = CHIP.get(level.lower(), ("#344054", "#ECEFF3"))[0]
    return Paragraph(f'<font color="{fg}"><b>{clean(level.capitalize())}</b></font>', ParagraphStyle("c", parent=ST["cell"], alignment=1))


def tiles(items: list, per_row: int | None = None) -> Table:
    """Cartes de chiffres clés : [(libellé, valeur, sous-texte, couleur)]."""
    n = per_row or len(items)
    gap = 0.35 * cm
    tw = (W - gap * (n - 1)) / n
    cells, widths, cmds = [], [], []
    for k, (lab, val, sub, col) in enumerate(items):
        cells.append([Paragraph(clean(lab.upper()), ST["lab"]),
                      Paragraph(f'<font color="{col}">{clean(val)}</font>', ParagraphStyle("v", parent=ST["h1"], fontSize=21, leading=26)),
                      Paragraph(clean(sub), ST["small"])])
        widths.append(tw)
        c = len(cells) - 1
        cmds += [("BACKGROUND", (c, 0), (c, 0), PALE), ("LINEABOVE", (c, 0), (c, 0), 2.6, colors.HexColor(col))]
        if k < n - 1:
            cells.append("")
            widths.append(gap)
    t = Table([cells], colWidths=widths)
    t.setStyle(TableStyle(cmds + [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 8),
                                  ("BOTTOMPADDING", (0, 0), (-1, -1), 8), ("LEFTPADDING", (0, 0), (-1, -1), 9)]))
    return t


# --------------------------------------------------------------------------- #
# Graphiques (palette identique au rapport)
# --------------------------------------------------------------------------- #
def _style_axes(ax):
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color("#9AA5B1")
    ax.tick_params(colors="#344054", labelsize=8)
    ax.grid(axis="x", color="#E4E7EC", lw=0.7)
    ax.set_axisbelow(True)


def _save(fig, path):
    fig.tight_layout()
    fig.savefig(path, dpi=170)
    plt.close(fig)


def _chart_scores(sites, path):
    fig, ax = plt.subplots(figsize=(7, 0.55 * len(sites) + 1.1))
    names = [s["domain"] for s in sites][::-1]
    vals = [s["reco"].get("health_score") for s in sites][::-1]
    bars = ax.barh(names, [v or 0 for v in vals], color=[band(v) for v in vals], height=0.55)
    ax.set_xlim(0, 108)
    ax.axvline(50, color="#9AA5B1", ls=":", lw=1)
    ax.axvline(75, color="#9AA5B1", ls=":", lw=1)
    for b, v in zip(bars, vals):
        ax.text((v or 0) + 1.5, b.get_y() + b.get_height() / 2, "N/D" if v is None else f"{v}", va="center", fontweight="bold", color="#0B2545")
    ax.set_title("Score de santé SEO (/100)", loc="left", fontsize=10, color="#0B2545", fontweight="bold")
    _style_axes(ax)
    _save(fig, path)


def _chart_perf(sites, path):
    fig, ax = plt.subplots(figsize=(7, 3))
    n, w = len(sites), 0.36
    for k, (dev, col) in enumerate((("mobile", "#0B2545"), ("desktop", "#7FB0E8"))):
        vals = [((device(s, dev) or {}).get("score")) for s in sites]
        xs = [i + (k - 0.5) * w for i in range(n)]
        bars = ax.bar(xs, [v or 0 for v in vals], w, label=dev.capitalize(), color=col)
        for b, v in zip(bars, vals):
            ax.text(b.get_x() + b.get_width() / 2, (v or 0) + 1.5, "N/D" if v is None else str(v), ha="center", fontsize=8, color="#344054")
    ax.axhline(90, color="#1F9D6B", ls="--", lw=1)
    ax.axhline(50, color="#C0392B", ls="--", lw=1)
    ax.set_xticks(range(n))
    ax.set_xticklabels([s["domain"] for s in sites])
    ax.set_ylim(0, 112)
    ax.legend(frameon=False, fontsize=8, loc="upper right", ncol=2)
    ax.set_title("Score de performance Lighthouse (/100) : seuils 50 et 90", loc="left", fontsize=10, color="#0B2545", fontweight="bold")
    _style_axes(ax)
    ax.grid(axis="x", visible=False)
    ax.grid(axis="y", color="#E4E7EC", lw=0.7)
    _save(fig, path)


def _chart_cwv(sites, path):
    fig, axes = plt.subplots(1, 3, figsize=(7.4, 2.7))
    for ax, (name, ttl, good, poor) in zip(axes, (("LCP", "LCP mobile (s)", 2.5, 4.0), ("CLS", "CLS mobile", 0.1, 0.25), ("TBT", "TBT mobile (ms)", 200, 600))):
        items = [next(i for i in cwv_items(device(s, "mobile")) if i["name"] == name) for s in sites]
        col = {"bon": "#1F9D6B", "à améliorer": "#D98E04", "mauvais": "#C0392B", None: "#9AA5B1"}
        ax.bar(range(len(sites)), [i["raw"] or 0 for i in items], color=[col[i["rating"]] for i in items], width=0.55)
        ax.axhline(good, color="#1F9D6B", ls="--", lw=1)
        ax.axhline(poor, color="#C0392B", ls="--", lw=1)
        ax.set_xticks(range(len(sites)))
        ax.set_xticklabels([s["domain"] for s in sites], fontsize=6.5, rotation=25, ha="right")
        ax.set_title(ttl, fontsize=9, color="#0B2545", fontweight="bold", loc="left")
        _style_axes(ax)
        ax.grid(axis="x", visible=False)
    fig.text(0.01, 0.0, "Tirets verts : seuil « bon » ; tirets rouges : seuil « mauvais » (Google).", fontsize=7, color="#5B6B7F")
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(path, dpi=170)
    plt.close(fig)


def _chart_issues(site, path):
    issues = build_issues(site)
    fig, ax = plt.subplots(figsize=(7, 1.9))
    cats = list(CATEGORIES.items())[::-1]
    left = [0] * len(cats)
    for sev, col in ((HIGH, "#C0392B"), (MED, "#D98E04"), (LOW, "#1F9D6B")):
        vals = [sum(1 for i in issues if i["category"] == k and i["severity"] == sev) for k, _ in cats]
        ax.barh([v for _, v in cats], vals, left=left, color=col, height=0.55, label=sev)
        left = [a + b for a, b in zip(left, vals)]
    for y, tot in enumerate(left):
        ax.text(tot + 0.3, y, str(tot), va="center", fontsize=8, color="#344054")
    ax.set_xlim(0, max(left + [1]) * 1.18 + 0.5)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend(frameon=False, fontsize=7.5, ncol=3, loc="lower right")
    ax.set_title("Problèmes détectés par catégorie et par gravité", loc="left", fontsize=9.5, color="#0B2545", fontweight="bold")
    _style_axes(ax)
    _save(fig, path)


# --------------------------------------------------------------------------- #
# Analyses utilisées dans le texte du rapport
# --------------------------------------------------------------------------- #
def issue_family(text: str) -> str:
    """« Réponse serveur lente (5,5 s) » -> « Réponse serveur lente » (pour regrouper les occurrences)."""
    s = re.sub(r"\(.*?\)", "", text.replace("(s)", "s"))
    return re.sub(r"\s+", " ", re.sub(r"\b\d+\b", "", s)).strip()


def top_problems(site: dict, n: int = 8) -> list[dict]:
    groups: dict = {}
    for i in build_issues(site):
        g = groups.setdefault(issue_family(i["issue"]), {"issue": issue_family(i["issue"]), "n": 0, "sev": i["severity"], "ex": i["url"]})
        g["n"] += 1
        if SEV_ORDER[i["severity"]] < SEV_ORDER[g["sev"]]:
            g["sev"] = i["severity"]
    return sorted(groups.values(), key=lambda g: (SEV_ORDER[g["sev"]], -g["n"]))[:n]


def site_insights(s: dict) -> tuple[list, list]:
    """Points forts et points à surveiller d'un site, déduits des résultats de l'audit."""
    a, summ = s["audit"], summary_of(s)
    h, rb, sm = a.get("https") or {}, a.get("robots") or {}, a.get("sitemap") or {}
    good, watch = [], []
    ok = https_ok(s)
    if ok:
        good.append("HTTPS valide" + (f" (certificat valable encore {h['cert_days_left']} jours)" if h.get("cert_days_left") is not None else ""))
    elif ok is False:
        watch.append("HTTPS indisponible ou certificat invalide")
    (good if rb.get("exists") else watch).append("robots.txt présent" if rb.get("exists") else "robots.txt absent")
    (good if sm.get("exists") else watch).append(f"Sitemap présent ({sm.get('url_count', 0)} URL)" if sm.get("exists") else "Sitemap introuvable")
    nb = summ.get("broken_links", 0)
    (watch if nb else good).append(f"{nb} lien(s) cassé(s) détecté(s)" if nb else "Aucun lien cassé détecté")
    miss = summ.get("missing_title", 0) + summ.get("missing_meta", 0) + summ.get("missing_h1", 0)
    (watch if miss else good).append(f"{miss} balise(s) title, meta ou H1 manquante(s)" if miss else "Balisage de base complet (title, meta, H1)")
    ttfb = summ.get("avg_ttfb_s")
    if ttfb is not None:
        (good if ttfb <= 0.8 else watch).append(f"Temps de réponse moyen du serveur : {fr(ttfb, 2)} s")
    m = device(s, "mobile")
    if m and m.get("score") is not None:
        (good if m["score"] >= 90 else watch).append(f"Performance mobile : {m['score']}/100")
    return good, watch


def common_actions(sites: list[dict]) -> list[dict]:
    agg: dict = {}
    for s in sites:
        for r in s["reco"].get("recommendations") or []:
            a = agg.setdefault(r["id"], {"r": r, "sites": set(), "sev": 0})
            a["sites"].add(s["domain"])
            if r["severity"] > a["sev"]:  # on garde la version la plus grave de la recommandation
                a["sev"], a["r"] = r["severity"], r
    return sorted(agg.values(), key=lambda a: (-a["sev"], -len(a["sites"]), a["r"]["effort_level"]))


def colored(text: str, is_score: bool = False) -> Paragraph:
    """Colore Oui / Non et, pour les lignes de score, la valeur selon sa qualité."""
    t, col = str(text), None
    if t == "Oui":
        col = GOOD
    elif t == "Non":
        col = BAD
    elif is_score:
        m = re.match(r"^(\d{1,3})", t)
        col = band(int(m.group(1))) if m else None
    return Paragraph(f'<font color="{col}"><b>{clean(t)}</b></font>' if col else clean(t), ST["cell"])


# --------------------------------------------------------------------------- #
# Page de garde, en-têtes et pieds de page
# --------------------------------------------------------------------------- #
class NumberedCanvas(canvas.Canvas):
    """Canvas qui sait le nombre total de pages (« Page 3 / 9 ») et dessine l'en-tête et le pied de page."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            if self._pageNumber > 1:
                self._chrome(total)
            super().showPage()
        super().save()

    def _chrome(self, total):
        w, h = A4
        self.setStrokeColor(LINE)
        self.setLineWidth(0.6)
        self.line(2 * cm, h - 1.6 * cm, w - 2 * cm, h - 1.6 * cm)
        self.line(2 * cm, 1.55 * cm, w - 2 * cm, 1.55 * cm)
        self.setFont("Helvetica-Bold", 7.8)
        self.setFillColor(NAVY)
        self.drawString(2 * cm, h - 1.35 * cm, "AUDIT SEO TECHNIQUE")
        self.setFont("Helvetica", 7.8)
        self.setFillColor(GREY)
        self.drawRightString(w - 2 * cm, h - 1.35 * cm, plain(HEADER_RIGHT[0]))
        self.drawString(2 * cm, 1.05 * cm, plain(COURSE))
        self.drawRightString(w - 2 * cm, 1.05 * cm, f"Page {self._pageNumber} / {total}")


def _cover(sites):
    def draw(c, doc):
        w, h = A4
        c.saveState()
        c.setFillColor(NAVY)
        c.rect(0, h - 15.2 * cm, w, 15.2 * cm, stroke=0, fill=1)
        c.setFillColor(BLUE)
        c.rect(0, h - 15.2 * cm, w, 0.22 * cm, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#7FB0E8"))
        c.setFont("Helvetica-Bold", 9)
        c.drawString(2.2 * cm, h - 3.1 * cm, "RAPPORT D'AUDIT")
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 40)
        c.drawString(2.2 * cm, h - 6.3 * cm, "Audit SEO")
        c.drawString(2.2 * cm, h - 8.1 * cm, "technique")
        c.setFont("Helvetica", 14)
        c.setFillColor(colors.HexColor("#CFE0F5"))
        c.drawString(2.2 * cm, h - 10.2 * cm, plain(f"Analyse comparative automatisée de {len(sites)} sites marocains" if len(sites) > 1
                                                    else f"Rapport d'audit automatisé de {sites[0]['domain']}"))
        c.setFont("Helvetica", 10)
        c.drawString(2.2 * cm, h - 11.1 * cm, "Exploration technique, performance PageSpeed et plan d'action priorisé")
        c.setFillColor(GREY)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(2.2 * cm, h - 17 * cm, "SITES ANALYSÉS")
        y = h - 18.1 * cm
        for s in sites[:6]:
            c.setFillColor(NAVY)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(2.2 * cm, y, plain(s["domain"]))
            c.setFillColor(GREY)
            c.setFont("Helvetica", 8.5)
            c.drawString(2.2 * cm, y - 0.45 * cm, plain(str(s["audit"].get("url", ""))[:70]))
            c.drawRightString(w - 2.2 * cm, y, f"Audit du {str(s['audit'].get('audited_at', 'N/D'))[:10]}")
            y -= 1.25 * cm
        c.setStrokeColor(LINE)
        c.line(2.2 * cm, 3.1 * cm, w - 2.2 * cm, 3.1 * cm)
        c.setFillColor(NAVY)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(2.2 * cm, 2.4 * cm, plain(COURSE))
        c.setFillColor(GREY)
        c.setFont("Helvetica", 8.5)
        c.drawString(2.2 * cm, 1.8 * cm, plain(f"Rapport généré automatiquement le {date.today():%d/%m/%Y}" + (f" · {AUTHORS}" if AUTHORS else "")))
        c.restoreState()
    return draw


# --------------------------------------------------------------------------- #
# Génération
# --------------------------------------------------------------------------- #
GROUPS = [
    ("Vue d'ensemble", ["Score de santé SEO", "Pages explorées", "Recommandations haute priorité"]),
    ("Indexation et sécurité", ["HTTPS valide", "Redirection http vers https", "robots.txt présent", "Sitemap présent", "Temps de réponse moyen"]),
    ("Contenu et liens", ["Problèmes de title", "Problèmes de meta description", "Pages sans H1 / à H1 multiples", "Images sans alt", "Liens cassés"]),
    ("Performance (PageSpeed)", ["Performance mobile (/100)", "Performance desktop (/100)", "LCP mobile", "CLS mobile", "TBT mobile",
                                 "Accessibilité mobile (/100)", "Bonnes pratiques mobile (/100)"]),
]


def generate_pdf(sites: list[dict], output_path: str | Path) -> Path:
    """Génère le rapport PDF complet pour les fiches site données."""
    if not sites:
        raise ValueError("Aucun site à inclure dans le rapport.")
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    N = len(sites)
    multi = N > 1
    ch = iter(range(1, 20))  # numéros de chapitres
    HEADER_RIGHT[0] = (f"Analyse comparative · {date.today():%d/%m/%Y}" if multi
                       else f"Rapport d'audit · {sites[0]['domain']} · {date.today():%d/%m/%Y}")
    story: list = [PageBreak()]
    has_perf = any(device(s, "mobile") for s in sites)

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        _chart_scores(sites, tmp / "scores.png")
        if has_perf:
            _chart_perf(sites, tmp / "perf.png")
            _chart_cwv(sites, tmp / "cwv.png")
        img = lambda name, ratio: Image(str(tmp / name), width=W, height=W * ratio)

        # ---- 1. Synthèse exécutive
        scored = sorted([s for s in sites if s["reco"].get("health_score") is not None], key=lambda s: -s["reco"]["health_score"])
        story += title(next(ch), "Synthèse exécutive", "Principaux enseignements de l'audit et positionnement de chaque site.")
        msgs = []
        if len(scored) >= 2:
            b, w_ = scored[0], scored[-1]
            msgs.append(f"{b['domain']} obtient le meilleur score de santé SEO ({b['reco']['health_score']}/100, note {b['reco']['grade']}) ; "
                        f"{w_['domain']} est le plus en retard ({w_['reco']['health_score']}/100, note {w_['reco']['grade']}).")
        elif scored:
            msgs.append(f"{scored[0]['domain']} obtient un score de santé SEO de {scored[0]['reco']['health_score']}/100 (note {scored[0]['reco']['grade']}).")
        hi = sum(s["reco"].get("counts", {}).get("haute", 0) for s in sites)
        qw = sum(1 for s in sites for r in s["reco"].get("recommendations") or [] if r["quick_win"])
        msgs.append(f"{hi} recommandation(s) de priorité haute restent ouvertes sur l'ensemble des sites. {qw} gain(s) rapide(s) "
                    "(priorité haute ou moyenne, effort faible) peuvent être traités en premier.")
        fam = Counter(issue_family(i["issue"]) for s in sites for i in build_issues(s))
        if fam:
            name, cnt = fam.most_common(1)[0]
            msgs.append(f"Le défaut le plus fréquent est « {name} » ({cnt} occurrences au total).")
        mob = [(s["domain"], d["score"]) for s in sites if (d := device(s, "mobile")) and d.get("score") is not None]
        if mob:
            msgs.append(f"Sur mobile, le score de performance Lighthouse va de {min(v for _, v in mob)} à {max(v for _, v in mob)}/100 ; "
                        + ("aucun site n'atteint le seuil « bon » de 90." if max(v for _, v in mob) < 90 else "certains sites dépassent le seuil « bon » de 90."))
        else:
            msgs.append("Aucune mesure PageSpeed n'est disponible : la performance n'est pas évaluée dans ce rapport.")
        story += bullets(msgs) + [Spacer(1, 10)]

        items = [(s["domain"], f"{s['reco']['health_score']}/100" if s["reco"].get("health_score") is not None else "N/D",
                  f"Note {s['reco'].get('grade') or 'N/D'} · {sum(s['reco'].get('counts', {}).values())} recommandations",
                  band(s["reco"].get("health_score"))) for s in sites]
        for k in range(0, len(items), 3):
            story += [tiles(items[k:k + 3], per_row=min(3, len(items))), Spacer(1, 8)]
        story += ([Spacer(1, 4), img("scores.png", (0.55 * N + 1.1) / 7)] if multi else []) + [PageBreak()]

        if multi:
            # ---- 2. Comparatif détaillé
            story += title(next(ch), "Comparatif détaillé", "Les mêmes critères, mesurés de la même façon sur chaque site.")
            data = dict(comparison_rows(sites))
            rows, extra = [[P("Critère", "head")] + [P(s["domain"], "head") for s in sites]], []
            for gname, labels in GROUPS:
                rows.append([P(gname.upper(), "lab", textColor=NAVY)] + [""] * N)
                extra += [("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), PALE), ("SPAN", (0, len(rows) - 1), (-1, len(rows) - 1))]
                for lab in labels:
                    if lab in data:
                        rows.append([P(lab)] + [colored(v, lab.startswith('Score') or '/100' in lab) for v in data[lab]])
            first = 5.4 * cm
            story += [tbl(rows, [first] + [(W - first) / N] * N, extra), Spacer(1, 6),
                      P("Les scores de santé sont des indicateurs internes au projet (pas des scores Google). Vert : bon ; orange : à améliorer ; rouge : mauvais.", "small"),
                      PageBreak()]

        # ---- 3. Performance
        story += title(next(ch), "Performance et Core Web Vitals", "Mesures Google PageSpeed Insights (Lighthouse), sur mobile et sur ordinateur.")
        if has_perf:
            story += [img("perf.png", 3 / 7), Spacer(1, 6), img("cwv.png", 2.7 / 7.4), Spacer(1, 8)]
            rows, extra = [[P(h, "head") for h in ["Site", "Appareil", "Score", "LCP", "INP (terrain)", "CLS", "TBT", "Access.", "Bonnes pr."]]], []
            for s in sites:
                for dev in ("mobile", "desktop"):
                    b = device(s, dev)
                    if not b:
                        rows.append([P(s["domain"], "cellb"), P(dev.capitalize())] + [P("N/D")] * 7)
                        continue
                    c = {i["name"]: i for i in cwv_items(b)}
                    rows.append([P(s["domain"], "cellb"), P(dev.capitalize()), P(b.get("score", "N/D"), "cellb")]
                                + [P(c[n]["value"]) for n in ("LCP", "INP", "CLS", "TBT")]
                                + [P((b.get("accessibility") or {}).get("score", "N/D")), P((b.get("best_practices") or {}).get("score", "N/D"))])
                    r = len(rows) - 1
                    for col, n in zip((3, 4, 5, 6), ("LCP", "INP", "CLS", "TBT")):
                        extra.append(("BACKGROUND", (col, r), (col, r), colors.HexColor(RATING_BG[c[n]["rating"]])))
            story += [tbl(rows, [3.0 * cm, 1.7 * cm, 1.2 * cm, 1.8 * cm, 2.3 * cm, 1.4 * cm, 1.8 * cm, 1.9 * cm, 1.9 * cm], extra), Spacer(1, 6),
                      P("Fond vert : bon ; orange : à améliorer ; rouge : mauvais. L'INP (réactivité) n'existe qu'en données terrain, le TBT en est l'équivalent "
                        "de laboratoire.", "small")]
        else:
            story.append(P("Aucune mesure PageSpeed exploitable : lancez python -m src.performance <url> pour chaque site afin de compléter cette section.", "body"))
        story.append(PageBreak())

        # ---- Un chapitre par site
        c_site = next(ch)
        for k, s in enumerate(sites, 1):
            label = f"{c_site}.{k}" if multi else f"{c_site}"
            r, summ, a = s["reco"], summary_of(s), s["audit"]
            story += [Paragraph(f'<font color="#1D4ED8">{label}</font>&nbsp;{clean(s["domain"])}', ST["h1"]),
                      HRFlowable(width="100%", thickness=1.4, color=BLUE, spaceBefore=3, spaceAfter=4),
                      P(f"{a.get('url', '')} · audit du {str(a.get('audited_at', 'N/D'))[:10]}", "sub")]
            if "error" in a:
                story += [P(f"Audit impossible : {a['error']}", "body"), PageBreak()]
                continue
            cnt = r.get("counts") or {}
            sc = r.get("health_score")
            story += [tiles([("Score de santé", f"{sc}/100" if sc is not None else "N/D", f"Note {r.get('grade') or 'N/D'}", band(sc)),
                             ("Pages explorées", str(summ.get("pages_crawled", 0)), f"{summ.get('pages_indexable', 0)} indexables", "#0B2545"),
                             ("Problèmes détectés", str(len(build_issues(s))), "toutes catégories", "#D98E04"),
                             ("Priorité haute", str(cnt.get("haute", 0)), "recommandations", "#C0392B" if cnt.get("haute") else GOOD)]),
                      Spacer(1, 10)]
            good, watch = site_insights(s)
            two = Table([[[P("POINTS FORTS", "lab", textColor=colors.HexColor(GOOD))] + bullets(good or ["Aucun point fort relevé."]),
                          [P("POINTS À SURVEILLER", "lab", textColor=colors.HexColor(BAD))] + bullets(watch or ["Rien à signaler."])]],
                        colWidths=[W / 2, W / 2])
            two.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BACKGROUND", (0, 0), (-1, -1), PALE),
                                     ("LEFTPADDING", (0, 0), (-1, -1), 10), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            story += [two, Spacer(1, 6)]
            _chart_issues(s, tmp / f"issues{k}.png")
            story += [img(f"issues{k}.png", 1.9 / 7)]

            tp = top_problems(s, 6)
            story += [CondPageBreak(5 * cm), Paragraph("Principaux problèmes", ST["h2"])]
            if tp:
                rows, extra = [[P(h, "head") for h in ["Problème", "Occurrences", "Gravité", "Exemple (URL)"]]], []
                for g in tp:
                    rows.append([P(g["issue"]), P(g["n"]), chip(g["sev"]), P(g["ex"], "small")])
                    extra += chip_cmd(2, len(rows) - 1, g["sev"])
                story.append(tbl(rows, [6.2 * cm, 2.2 * cm, 2.2 * cm, W - 10.6 * cm], extra))
            else:
                story.append(P("Aucun problème détecté.", "body"))

            story += [PageBreak(),
                      Paragraph(f'<font color="#1D4ED8">{label}</font>&nbsp;{clean(s["domain"])} <font color="#5B6B7F" size="12">· Plan d\'action priorisé</font>', ST["h1"]),
                      HRFlowable(width="100%", thickness=1.4, color=BLUE, spaceBefore=3, spaceAfter=8),
                      P("Recommandations fusionnées (audit + PageSpeed), classées de la plus urgente à la moins urgente.", "sub")]
            recs = sorted(r.get("recommendations") or [], key=lambda x: (PRIO_ORDER.get(x["priority"], 3), x["rank"]))
            if recs:
                rows, extra = [[P(h, "head") for h in ["#", "Priorité", "Recommandation", "Action à mener", "Effort"]]], []
                for x in recs[:MAX_RECO_PER_SITE]:
                    rows.append([P(x["rank"]), chip(x["priority"]),
                                 Paragraph(f"<b>{clean(x['title'])}</b><br/><font color='#5B6B7F' size='7.5'>{clean(x['category'])}</font>", ST["cell"]),
                                 P(x["action"]), P(x["effort"] + (" · gain rapide" if x.get("quick_win") else ""))])
                    extra += chip_cmd(1, len(rows) - 1, x["priority"])
                story.append(tbl(rows, [0.8 * cm, 1.7 * cm, 4.6 * cm, W - 10.2 * cm, 3.1 * cm], extra))
                if len(recs) > MAX_RECO_PER_SITE:
                    story.append(P(f"+ {len(recs) - MAX_RECO_PER_SITE} autre(s) recommandation(s) de moindre priorité (voir results/{s['domain']}_recommendations.json).", "small", spaceBefore=4))
            else:
                story.append(P(r.get("error") or "Aucune recommandation.", "body"))
            story.append(PageBreak())

        # ---- 5. Chantiers prioritaires
        story += title(next(ch), "Chantiers prioritaires", "Actions qui reviennent sur plusieurs sites ou qui ont le plus d'impact : par où commencer." if multi
                       else "Les actions à plus fort impact : par où commencer.")
        top = common_actions(sites)[:8]
        if top:
            story.append(Paragraph("Trois recommandations à lancer en premier", ST["h2"]))
            for n, t in enumerate(top[:3], 1):
                rr = t["r"]
                story.append(KeepTogether([Paragraph(f'<font color="#1D4ED8"><b>{n}.</b></font>&nbsp;<b>{clean(rr["title"])}</b>'
                                                     f' <font color="#5B6B7F">({(str(len(t["sites"])) + "/" + str(N) + " site(s) · ") if multi else ""}effort {clean(rr["effort"])})</font>', ST["body"]),
                                           P(rr["action"], "small", leftIndent=11, spaceAfter=6, fontSize=8.6, leading=12)]))
            story.append(Paragraph("Vue d'ensemble des chantiers", ST["h2"]))
            heads = ["Recommandation", "Priorité"] + (["Sites"] if multi else []) + ["Effort"]
            rows, extra = [[P(h, "head") for h in heads]], []
            for t in top:
                rr = t["r"]
                rows.append([P(rr["title"]), chip(rr["priority"])] + ([P(f"{len(t['sites'])}/{N}")] if multi else []) + [P(rr["effort"])])
                extra += chip_cmd(1, len(rows) - 1, rr["priority"])
            story.append(tbl(rows, [W - 8 * cm, 2.2 * cm, 1.6 * cm, 4.2 * cm] if multi else [W - 6.4 * cm, 2.2 * cm, 4.2 * cm], extra))
        else:
            story.append(P("Aucune recommandation à synthétiser.", "body"))
        story.append(PageBreak())

        # ---- 6. Méthode, limites et glossaire
        story += title(next(ch), "Méthode, limites et glossaire")
        story.append(Paragraph("Méthode", ST["h2"]))
        story += bullets([
            "Exploration automatisée d'un échantillon de pages de chaque site : robots.txt, sitemap, balises title, meta description et H1, images sans attribut alt, liens cassés, HTTPS et temps de réponse.",
            "Mesure de la performance avec l'API Google PageSpeed Insights (Lighthouse) sur mobile et sur ordinateur : Core Web Vitals, accessibilité, bonnes pratiques.",
            "Fusion des constats en recommandations sans doublon, classées par priorité (haute, moyenne, basse) selon la gravité et l'ampleur réelle du problème.",
            "Le score de santé (/100) retire des points selon la priorité de chaque recommandation ouverte. C'est un indicateur interne au projet, pas un score Google."])
        story.append(Paragraph("Limites", ST["h2"]))
        notes = sorted({n for s in sites for n in s["reco"].get("notes") or []})
        story += bullets(["L'exploration porte sur un échantillon de pages : les chiffres sont indicatifs pour l'ensemble du site.",
                          "Les mesures de performance varient d'un test à l'autre et reflètent les conditions du moment.",
                          "L'audit ne couvre ni la qualité du contenu, ni les backlinks, ni le SEO local."] + notes)
        story.append(Paragraph("Glossaire", ST["h2"]))
        gl = [("LCP", "Largest Contentful Paint : temps d'affichage du plus gros élément visible. Bon : 2,5 s ou moins."),
              ("INP", "Interaction to Next Paint : réactivité aux clics, mesurée sur de vrais visiteurs. Bon : 200 ms ou moins."),
              ("CLS", "Cumulative Layout Shift : stabilité visuelle (la page « saute »). Bon : 0,1 ou moins."),
              ("TBT", "Total Blocking Time : durée pendant laquelle la page est figée au chargement. Bon : 200 ms ou moins."),
              ("Sitemap", "Plan du site transmis aux moteurs de recherche pour faciliter l'exploration."),
              ("Noindex", "Balise demandant aux moteurs de ne pas indexer une page.")]
        story.append(tbl([[P("Terme", "head"), P("Définition", "head")]] + [[P(a, "cellb"), P(b)] for a, b in gl], [2.6 * cm, W - 2.6 * cm]))

        SimpleDocTemplate(str(out), pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2.3 * cm, bottomMargin=2.1 * cm,
                          title="Audit SEO technique", author=AUTHORS or "TDIA3 - ENSAH").build(
            story, onFirstPage=_cover(sites), canvasmaker=NumberedCanvas)
    return out


PRIO_ORDER = {"haute": 0, "moyenne": 1, "basse": 2}

if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "results"
    target = sys.argv[2] if len(sys.argv) > 2 else f"reports/rapport_seo_{date.today()}.pdf"
    print("Rapport généré :", generate_pdf(load_sites(folder), target))