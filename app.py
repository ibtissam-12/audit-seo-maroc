"""
app.py : dashboard Streamlit « SEO Audit Maroc » (audit SEO technique + PageSpeed + recommandations).

Lancement :  streamlit run app.py
Données   :  results/<domaine>.json (audit.py), results/<domaine>_performance.json (performance.py),
             recommandations calculées par recommendations.py. Tout est lu via src/report.py.
"""
import json
import os
from datetime import date
from html import escape
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.report import (CATEGORIES, build_issues, comparison_rows, cwv_items, device, fr,
                        generate_pdf, https_ok, indicator_rows, load_sites, summary_of)

# Modules de l'équipe : l'app reste utilisable en lecture seule s'ils ne sont pas encore fusionnés.
try:
    from src.audit import audit_site
except ImportError:
    audit_site = None
try:
    from src.performance import performance_site
except ImportError:
    performance_site = None

load_dotenv()
RESULTS_DIR, REPORTS_DIR = Path("results"), Path("reports")
RESULTS_DIR.mkdir(exist_ok=True)
REPORTS_DIR.mkdir(exist_ok=True)
MAX_PAGES = 10  # pages explorées lors d'un audit lancé depuis l'interface

st.set_page_config(page_title="SEO Audit Maroc", page_icon="🔎", layout="wide")

# --------------------------------------------------------------------------- #
# Style : fond bleu très clair, cartes blanches, accent bleu (#2563EB), pastilles de gravité
# --------------------------------------------------------------------------- #
st.markdown("""<style>
:root{--accent:#2563EB;--ink:#0F2744;--muted:#5B6B7F;--line:#E3EAF3}
.stApp{background:#F5F8FC}
.block-container{padding-top:4.5rem;max-width:1400px}
[data-testid="stHeader"]{background:transparent}
[data-testid="stAppDeployButton"]{display:none}
section[data-testid="stSidebar"]{background:#fff;border-right:1px solid var(--line)}
[data-testid="stPageLink-NavLink"]{border-radius:10px;padding:.45rem .7rem}
[data-testid="stPageLink-NavLink"][aria-current="page"]{background:#E8F0FE}
[data-testid="stPageLink-NavLink"][aria-current="page"] *{color:var(--accent)!important;font-weight:600}
[class*="st-key-card-"]{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 20px;box-shadow:0 1px 2px rgba(15,39,68,.04)}
button[kind="primary"]{background:var(--accent);border-color:var(--accent);color:#fff;border-radius:8px}
button[kind="primary"]:hover{background:#1D4ED8;border-color:#1D4ED8;color:#fff}
button[data-baseweb="tab"][aria-selected="true"]{color:var(--accent)}
[data-baseweb="tab-highlight"]{background-color:var(--accent)!important}
[data-baseweb="tag"]{background-color:#E8F0FE!important}
[data-baseweb="tag"] *{color:var(--accent)!important}
div[data-baseweb="input"]:focus-within,div[data-baseweb="select"]>div:focus-within{border-color:var(--accent)!important}
.page-title{font-size:1.7rem;font-weight:700;color:var(--ink);margin:0}
.page-sub{color:var(--muted);margin:0 0 1.1rem 0}
.head{display:flex;align-items:center;gap:12px;margin-bottom:10px}
.head .t{font-weight:700;color:var(--ink);font-size:1.05rem}
.head .s{color:var(--muted);font-size:.85rem;font-weight:400}
.ico{min-width:40px;height:40px;border-radius:10px;display:flex;align-items:center;justify-content:center}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:16px 0}
.kpi{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px 18px;display:flex;gap:14px;align-items:center;min-height:116px}
.kpi .l{color:var(--ink);font-weight:600;font-size:.9rem}
.kpi .v{font-size:1.9rem;font-weight:700;color:var(--ink);line-height:1.15}
.kpi .v small{font-size:1rem;color:#8A99AB;font-weight:500}
.kpi .s{color:#8A99AB;font-size:.72rem}
.tbl-wrap{overflow-x:auto}
.tbl{width:100%;border-collapse:collapse;font-size:.85rem}
.tbl th{background:#F3F6FA;color:#42526B;text-align:left;padding:8px 10px;font-weight:600;white-space:nowrap}
.tbl td{padding:9px 10px;border-bottom:1px solid #EEF2F7;color:#1B2B41;vertical-align:top}
.pill{display:inline-block;padding:2px 12px;border-radius:999px;font-size:.75rem;font-weight:600;white-space:nowrap}
.p-high{background:#FDE8E8;color:#C0392B}.p-med{background:#FFF1CC;color:#9A6B00}
.p-low{background:#DDF5E6;color:#2E8B57}.p-gray{background:#ECEFF3;color:#6B7A8D}
.cwv-grid{display:grid;gap:12px}
.cwv{border:1px solid var(--line);border-radius:12px;padding:14px}
.cwv .n{font-weight:700;font-size:.85rem;color:var(--ink)}
.cwv .v{font-size:1.6rem;font-weight:700;color:var(--ink);margin:4px 0 8px}
.cwv .d{color:var(--muted);font-size:.78rem;margin-top:10px}
.cwv .th{color:#8A99AB;font-size:.74rem;margin-top:6px}
.act{display:flex;align-items:center;gap:14px;padding:11px 4px;border-bottom:1px solid #EEF2F7}
.act .t{flex:1}.act .t b{color:var(--ink);font-size:.9rem}.act .t div{color:var(--muted);font-size:.78rem}
.act .m{color:var(--muted);font-size:.78rem;text-align:right;white-space:nowrap}
.note{background:#EEF4FB;border-radius:10px;padding:10px 14px;color:#42526B;font-size:.8rem;margin-top:12px}
.mini{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:12px}
.mini div{background:#F3F6FA;border-radius:10px;padding:10px 14px;color:var(--muted);font-size:.8rem}
.mini b{display:block;color:var(--ink);font-size:1.3rem}
.brand{display:flex;align-items:center;gap:10px;font-weight:700;font-size:1.15rem;color:var(--ink);margin:4px 0 14px}
.side-foot{text-align:center;color:#8A99AB;font-size:.75rem;margin-top:1.5rem}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}}
</style>""", unsafe_allow_html=True)

# --------------------------------------------------------------------------- #
# Briques HTML (icônes SVG, pastilles, tableaux, cartes)
# --------------------------------------------------------------------------- #
SVG = {
    "link": '<path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/>',
    "gauge": '<path d="M12 14l4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/>',
    "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
    "alert": '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "lock": '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    "code": '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
    "activity": '<polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>',
    "bulb": '<path d="M9 18h6"/><path d="M10 22h4"/><path d="M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.3h6c0-1 .4-1.8 1-2.3A7 7 0 0 0 12 2z"/>',
    "compare": '<polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>',
    "search": '<circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
}


def html(s: str) -> None:
    st.markdown(s, unsafe_allow_html=True)


def svg(name: str, color: str = "#2563EB", size: int = 20) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">{SVG[name]}</svg>')


def head(icon: str, title: str, sub: str = "") -> str:
    s = f'<div class="s">{escape(sub)}</div>' if sub else ""
    return (f'<div class="head"><div class="ico" style="background:#E8F0FE">{svg(icon)}</div>'
            f'<div><div class="t">{escape(title)}</div>{s}</div></div>')


def pill(text: str, cls: str) -> tuple:
    return (text, cls)


SEV_CLS = {"haute": "p-high", "moyenne": "p-med", "faible": "p-low", "basse": "p-low"}
RATING_CLS = {"bon": "p-low", "à améliorer": "p-med", "mauvais": "p-high", None: "p-gray"}


def sev_pill(sev: str) -> tuple:
    return (sev.capitalize(), SEV_CLS.get(sev.lower(), "p-gray"))


def table_html(headers: list, rows: list, max_h: int | None = None) -> str:
    """Tableau HTML ; une cellule (texte, classe) devient une pastille colorée."""
    def cell(c):
        return (f'<td><span class="pill {c[1]}">{escape(str(c[0]))}</span></td>' if isinstance(c, tuple)
                else f"<td>{escape(str(c))}</td>")
    body = "".join("<tr>" + "".join(cell(c) for c in r) + "</tr>" for r in rows)
    style = f' style="max-height:{max_h}px;overflow-y:auto"' if max_h else ""
    return (f'<div class="tbl-wrap"{style}><table class="tbl"><tr>' + "".join(f"<th>{escape(h)}</th>" for h in headers)
            + f"</tr>{body}</table></div>")


def empty(msg: str = "Aucun élément à afficher.") -> str:
    return f'<div class="note">{escape(msg)}</div>'


def kpi(icon: str, label: str, value, suffix: str, sub: str, bg: str, color: str) -> str:
    suf = f"<small> {escape(suffix)}</small>" if suffix else ""
    return (f'<div class="kpi"><div class="ico" style="background:{bg}">{svg(icon, color, 22)}</div><div>'
            f'<div class="l">{escape(label)}</div><div class="v">{escape(str(value))}{suf}</div>'
            f'<div class="s">{escape(sub)}</div></div></div>')


def cwv_html(block: dict | None, cols: int) -> str:
    if not block:
        return empty("Aucune mesure PageSpeed pour cet appareil.")
    cards = ""
    for it in cwv_items(block):
        r = it["rating"]
        cards += (f'<div class="cwv"><div class="n">{it["name"]}</div><div class="v">{escape(it["value"])}</div>'
                  f'<span class="pill {RATING_CLS[r]}">{(r or "N/D").capitalize()}</span>'
                  f'<div class="d">{escape(it["note"])}</div><div class="th">Seuil : {escape(it["threshold"])}</div></div>')
    return f'<div class="cwv-grid" style="grid-template-columns:repeat({cols},1fr)">{cards}</div>'


def actions_html(site: dict, limit: int | None = None, category: str | None = None) -> str:
    recs = [r for r in site["reco"].get("recommendations") or [] if category in (None, r["category"])][:limit]
    if not recs:
        return empty(site["reco"].get("error") or "Aucune recommandation.")
    out = ""
    for r in recs:
        why = r["why_it_matters"]
        why = why if len(why) < 120 else why[:117] + "..."
        count = f"{r['count']} concerné(s)<br>" if r.get("count") else ""
        out += (f'<div class="act"><span class="pill {SEV_CLS[r["priority"]]}">{r["priority"].capitalize()}</span>'
                f'<div class="t"><b>{escape(r["title"])}</b><div>{escape(why)}</div></div>'
                f'<div class="m">{count}Priorité : {r["rank"]}</div></div>')
    return out


# --------------------------------------------------------------------------- #
# Lancement d'un audit depuis l'interface
# --------------------------------------------------------------------------- #
def launch_audit(url: str) -> None:
    url = url.strip()
    if not url:
        st.error("Entrez l'adresse du site (ex. https://www.exemple.ma).")
        return
    if audit_site is None:
        st.warning("src/audit.py est introuvable : fusionnez la branche audit.")
        return
    with st.status("Audit en cours (quelques minutes)...", expanded=True) as status:
        st.write("Exploration du site...")
        audit = audit_site(url, max_pages=MAX_PAGES)
        domain = audit["domain"].replace(":", "_")
        (RESULTS_DIR / f"{domain}.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
        if "error" in audit:
            status.update(label=f"Site injoignable : {audit['error']}", state="error")
            return
        if performance_site:
            st.write("Mesure PageSpeed (mobile et desktop)...")
            perf = performance_site(url, api_key=os.getenv("PSI_API_KEY"))
            (RESULTS_DIR / f"{domain}_performance.json").write_text(
                json.dumps(perf, ensure_ascii=False, indent=2), encoding="utf-8")
            if "error" in perf:
                st.warning(f"PageSpeed : {perf['error']}")
        status.update(label="Audit terminé", state="complete")
    st.session_state["pending_site"] = audit["domain"]
    st.session_state["just_audited"] = audit["domain"]
    st.rerun()


# --------------------------------------------------------------------------- #
# Barre latérale et chargement des données
# --------------------------------------------------------------------------- #
PAGES = {}  # rempli plus bas : clé -> st.Page (utilisé par les liens « voir tout »)

with st.sidebar:
    html(f'<div class="brand">{svg("search", "#2563EB", 26)} SEO Audit Maroc</div>')
    nav_slot = st.container()
    st.divider()
    site_slot = st.container()
    with st.expander("Importer des résultats"):
        for f in st.file_uploader("Fichiers JSON", type="json", accept_multiple_files=True) or []:
            (RESULTS_DIR / f.name).write_bytes(f.getvalue())
    html('<div class="side-foot">SEO Audit Maroc<br>Better SEO. Greater visibility.</div>')

SITES = load_sites(RESULTS_DIR)
NAMES = [s["domain"] for s in SITES]
if "pending_site" in st.session_state:  # site qui vient d'être audité
    st.session_state["site_select"] = st.session_state.pop("pending_site")
if SITES:
    with site_slot:
        st.selectbox("Site analysé", NAMES, key="site_select")


def header(title: str, sub: str) -> None:
    html(f'<p class="page-title">{escape(title)}</p><p class="page-sub">{escape(sub)}</p>')


def current() -> dict:
    """Fiche du site sélectionné ; arrête la page s'il n'y a aucun résultat ou si l'audit a échoué."""
    if not SITES:
        st.info("Aucun résultat pour l'instant : lancez un audit depuis le tableau de bord ou importez des fichiers JSON.")
        st.stop()
    s = SITES[NAMES.index(st.session_state["site_select"])]
    if "error" in s["audit"]:
        st.error(f"Audit impossible pour {s['domain']} : {s['audit']['error']}")
        st.stop()
    state = (s["reco"].get("sources") or {}).get("performance", {})
    if state.get("status") != "ok":
        st.info("Mesure PageSpeed absente ou en erreur" + (f" ({state['detail']})" if state.get("detail") else "")
                + " : lancez `python -m src.performance <url>` pour ce site.")
    return s


# --------------------------------------------------------------------------- #
# Cartes réutilisables
# --------------------------------------------------------------------------- #
def audit_card() -> None:
    with st.container(border=False, key="card-audit"):
        html(head("link", "Audit SEO technique",
                  "Saisissez l'adresse d'un site : l'exploration, la mesure PageSpeed et les recommandations sont lancées "
                  "d'un seul coup. Vous pourrez ensuite générer le rapport du site."))
        c1, c2 = st.columns([5, 1.3])
        url = c1.text_input("Adresse du site", placeholder="https://www.exemple.ma", label_visibility="collapsed")
        if c2.button("Lancer l'audit", type="primary", width="stretch"):
            launch_audit(url)
        st.caption("L'audit peut prendre quelques instants. Veuillez patienter.")


def findings_card(site: dict, limit: int | None, height: int | None) -> None:
    issues = build_issues(site)
    with st.container(border=False, height=height or "content", key="card-findings"):
        html(head("code", "Constats SEO techniques", f"{len(issues)} problème(s) détecté(s)"))
        for tab, key in zip(st.tabs(list(CATEGORIES.values())), CATEGORIES):
            with tab:
                rows = [i for i in issues if i["category"] == key]
                html(table_html(["Problème", "URL concernée", "Gravité", "Statut"],
                                [[i["issue"], i["url"], sev_pill(i["severity"]), pill("Ouvert", "p-high")]
                                 for i in rows[:limit]]) if rows else empty("Aucun problème dans cette catégorie."))
                if limit and len(rows) > limit:
                    st.caption(f"Affichage de {limit} sur {len(rows)} problèmes.")
        if limit:
            st.page_link(PAGES["seo"], label="Voir tous les problèmes", icon=":material/arrow_forward:")


def export_card(height: int | None = None) -> None:
    """Rapport PDF du site sélectionné + accès à la comparaison de plusieurs sites."""
    site = SITES[NAMES.index(st.session_state["site_select"])] if SITES else None
    with st.container(border=False, height=height or "content", key="card-export"):
        html(head("file", "Rapport du site", "PDF structuré : constats techniques, performance et plan d'action priorisé du site sélectionné."))
        if st.button("Générer le rapport PDF", type="primary", width="stretch", disabled=not site or "error" in site["audit"]):
            path = generate_pdf([site], REPORTS_DIR / f"rapport_{site['domain'].replace(':', '_')}_{date.today()}.pdf")  # « : » interdit sous Windows
            st.session_state["pdf_site"] = (site["domain"], path.name, path.read_bytes())
        saved = st.session_state.get("pdf_site")
        if site and saved and saved[0] == site["domain"]:
            st.download_button("Télécharger le PDF", saved[2], file_name=saved[1], mime="application/pdf", type="primary", width="stretch")
        if st.button("Comparer plusieurs sites", width="stretch", disabled=len(SITES) < 2, key="goto_cmp",
                     help="Il faut au moins 2 sites audités." if len(SITES) < 2 else None):
            st.switch_page(PAGES["cmp"])
        if site:
            st.caption(f"{site['domain']}  ·  dernier audit : {str(site['audit'].get('audited_at', 'N/D'))[:10]}")


def comparison_export(chosen: list[str], key: str) -> None:
    """Bouton + téléchargement du rapport comparatif des sites choisis."""
    picked = [x for x in SITES if x["domain"] in chosen]
    if st.button("Générer le rapport comparatif", type="primary", width="stretch", disabled=len(picked) < 2, key=f"gen_cmp_{key}"):
        path = generate_pdf(picked, REPORTS_DIR / f"rapport_comparatif_{date.today()}.pdf")
        st.session_state["pdf_cmp"] = (path.name, path.read_bytes())
    if "pdf_cmp" in st.session_state:
        name, data = st.session_state["pdf_cmp"]
        st.download_button("Télécharger le rapport comparatif", data, file_name=name, mime="application/pdf",
                           type="primary", width="stretch", key=f"dl_cmp_{key}")


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def page_dashboard() -> None:
    header("Tableau de bord Audit SEO", "Analysez la santé technique SEO et la performance de votre site.")
    audit_card()
    done = st.session_state.pop("just_audited", None)
    if done:
        st.success(f"Audit de {done} terminé : consultez les résultats ci-dessous, puis générez le rapport du site.")
    s = current()
    reco, summ = s["reco"], summary_of(s)
    issues, ok = build_issues(s), https_ok(s)
    score = reco.get("health_score")
    cert = (s["audit"].get("https") or {}).get("cert_days_left")
    html('<div class="kpis">'
         + kpi("gauge", "Score SEO", "N/D" if score is None else score, "/100",
               f"Score interne au projet (pas un score Google officiel) · note {reco.get('grade') or 'N/D'}", "#E8F0FE", "#2563EB")
         + kpi("file", "Pages explorées", summ.get("pages_crawled", 0), "", "Total des pages analysées", "#E8F0FE", "#2563EB")
         + kpi("alert", "Problèmes détectés", len(issues), "", "Problèmes techniques trouvés", "#FFF1CC", "#D97706")
         + kpi("lock", "Statut HTTPS", "N/D" if ok is None else "Activé" if ok else "Problème", "",
               ("Connexion sécurisée" + (f" · certificat {cert} j" if cert is not None else "")) if ok else "Connexion non sécurisée",
               "#DDF5E6" if ok else "#FDE8E8", "#2E8B57" if ok else "#C0392B")
         + "</div>")

    left, right = st.columns([1.7, 1])
    with left:
        findings_card(s, limit=5, height=480)
    with right:
        with st.container(border=False, height=480, key="card-cwv"):
            html(head("activity", "Core Web Vitals", "Mobile · PageSpeed Insights (données de laboratoire)"))
            html(cwv_html(device(s, "mobile"), 2))
            html('<div class="note">Les données terrain réelles (dont l\'INP) peuvent être indisponibles pour les '
                 'sites à faible trafic : elles sont alors affichées « N/D ».</div>')
    left, right = st.columns([1.7, 1])
    with left:
        with st.container(border=False, height=380, key="card-actions"):
            html(head("bulb", "Actions recommandées"))
            html(actions_html(s, limit=4))
            st.page_link(PAGES["reco"], label="Voir toutes les recommandations", icon=":material/arrow_forward:")
    with right:
        export_card(height=380)


def page_seo() -> None:
    header("SEO technique", "Tous les problèmes détectés sur le site analysé.")
    s = current()
    findings_card(s, limit=None, height=None)
    with st.container(border=False, key="card-indicators"):
        html(head("gauge", "Indicateurs de l'audit"))
        html(table_html(["Indicateur", "Valeur", "Statut"], [
            [r["Indicateur"], r["Valeur"], pill(r["Statut"], {"OK": "p-low", "À corriger": "p-med"}.get(r["Statut"], "p-gray"))]
            for r in indicator_rows(s)]))
    with st.container(border=False, key="card-pages"):
        html(head("file", "Pages explorées", f"{len(s['audit'].get('pages') or [])} page(s)"))
        html(table_html(["URL", "HTTP", "Réponse (s)", "Title", "Meta", "H1", "Images sans alt", "Mots"], [
            [p.get("url", ""), p.get("status", "N/D"), p.get("ttfb_s", ""), p.get("title_len", ""), p.get("meta_len", ""),
             p.get("h1_count", ""), p.get("images_missing_alt", ""), p.get("word_count", "")]
            for p in s["audit"].get("pages") or []], max_h=420))


def page_perf() -> None:
    header("Performance", "Core Web Vitals, accessibilité et bonnes pratiques (Google PageSpeed Insights).")
    s = current()
    for dev in ("mobile", "desktop"):
        b = device(s, dev)
        with st.container(border=False, key=f"card-perf-{dev}"):
            html(head("activity", dev.capitalize(), "L'INP est une donnée terrain ; le TBT en est l'équivalent de laboratoire."))
            if b:
                html('<div class="mini">'
                     f'<div>Performance<b>{b.get("score", "N/D")} / 100</b></div>'
                     f'<div>Accessibilité<b>{(b.get("accessibility") or {}).get("score", "N/D")} / 100</b></div>'
                     f'<div>Bonnes pratiques<b>{(b.get("best_practices") or {}).get("score", "N/D")} / 100</b></div></div>')
            html(cwv_html(b, 4))
            opps = (b or {}).get("google_opportunities") or []
            if opps:
                html('<div class="note"><b>Principales opportunités proposées par Google</b></div>')
                html(table_html(["Opportunité", "Gain estimé"], [[o["title"], fr((o.get("savings_ms") or 0) / 1000, 1) + " s"]
                                                                 for o in opps]))


def page_reco() -> None:
    header("Recommandations", "Plan d'action fusionné (audit + PageSpeed), classé par priorité.")
    s = current()
    recs = s["reco"].get("recommendations") or []
    with st.container(border=False, key="card-reco"):
        html(head("bulb", "Actions recommandées", f"{len(recs)} recommandation(s)"))
        cats = sorted({r["category"] for r in recs})
        for tab, cat in zip(st.tabs(["Toutes"] + cats), [None] + cats):
            with tab:
                html(actions_html(s, category=cat))
    if recs:
        with st.container(border=False, key="card-detail"):
            html(head("file", "Détail d'une recommandation"))
            titles = {f"#{r['rank']} {r['title']}": r for r in recs}
            r = titles[st.selectbox("Recommandation", list(titles), label_visibility="collapsed")]
            st.markdown(f"**Problème.** {r['problem']}")
            st.markdown(f"**Pourquoi c'est important.** {r['why_it_matters']}")
            st.markdown(f"**Action.** {r['action']}")
            st.caption(f"Priorité {r['priority']} · effort {r['effort']} · sources : {', '.join(r['sources'])}"
                       + (f" · appareils : {', '.join(r['devices'])}" if r["devices"] else ""))
            for ex in r["examples"][:3]:
                st.code(ex, language=None)
            if r.get("learn_more"):
                st.markdown(f"[Documentation Lighthouse]({r['learn_more']})")
    for n in s["reco"].get("notes") or []:
        st.caption("Note : " + n)


def page_compare() -> None:
    header("Comparaison", "Choisissez les sites à comparer (au moins deux) puis générez le rapport comparatif.")
    if len(SITES) < 2:
        st.info("Il faut au moins 2 sites audités pour comparer : lancez un audit depuis le tableau de bord.")
        st.stop()
    with st.container(border=False, key="card-cmp-pick"):
        html(head("compare", "Sites à comparer"))
        chosen = st.multiselect("Sites à comparer", NAMES, default=NAMES[:3], label_visibility="collapsed", key="cmp_pick")
    picked = [x for x in SITES if x["domain"] in chosen]
    if len(picked) < 2:
        st.info("Sélectionnez au moins 2 sites.")
        st.stop()
    with st.container(border=False, key="card-cmp-chart"):
        html(head("gauge", "Score de santé SEO"))
        scores = pd.DataFrame({"Score": [x["reco"].get("health_score") or 0 for x in picked]}, index=[x["domain"] for x in picked])
        st.bar_chart(scores, horizontal=True, color="#2563EB")
    with st.container(border=False, key="card-cmp-table"):
        html(head("file", "Tableau comparatif"))
        html(table_html(["Critère"] + [x["domain"] for x in picked], [[label] + vals for label, vals in comparison_rows(picked)]))
    with st.container(border=False, key="card-cmp-export"):
        html(head("file", "Rapport comparatif", f"PDF comparant {len(picked)} sites : synthèse, comparatif, performance, détail et chantiers prioritaires."))
        comparison_export(chosen, "cmp")


def page_pdf() -> None:
    header("Rapports PDF", "Rapport d'un seul site ou rapport comparatif de plusieurs sites.")
    if not SITES:
        st.info("Aucun résultat pour l'instant : lancez un audit depuis le tableau de bord.")
        st.stop()
    export_card()
    with st.container(border=False, key="card-pdf-cmp"):
        html(head("compare", "Rapport comparatif", "Comparez plusieurs sites dans un seul PDF (au moins deux)."))
        chosen = st.multiselect("Sites à comparer", NAMES, default=NAMES[:3], label_visibility="collapsed", key="pdf_pick")
        comparison_export(chosen, "pdf")


# --------------------------------------------------------------------------- #
# Navigation (liens dans la barre latérale, pages natives Streamlit)
# --------------------------------------------------------------------------- #
PAGES.update(
    dash=st.Page(page_dashboard, title="Tableau de bord", icon=":material/dashboard:", default=True),
    seo=st.Page(page_seo, title="SEO technique", icon=":material/code:"),
    perf=st.Page(page_perf, title="Performance", icon=":material/speed:"),
    reco=st.Page(page_reco, title="Recommandations", icon=":material/lightbulb:"),
    cmp=st.Page(page_compare, title="Comparaison", icon=":material/compare_arrows:"),
    pdf=st.Page(page_pdf, title="Rapports PDF", icon=":material/description:"),
)
nav = st.navigation(list(PAGES.values()), position="hidden")
with nav_slot:
    for page in PAGES.values():
        st.page_link(page)
nav.run()
