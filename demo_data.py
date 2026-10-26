"""
demo_data.py : crée 3 sites FICTIFS dans results/ pour tester l'interface et le rapport sans lancer d'audit.

    python demo_data.py            -> crée results/demo-*.json (audit + performance, format des modules de l'équipe)
    python demo_data.py --clean    -> supprime tous les fichiers results/demo-*
Les données sont inventées : ne pas les utiliser dans le rapport final.
"""
import json
import sys
from pathlib import Path

from src.audit import build_summary  # même calcul de résumé que le vrai audit

RESULTS = Path("results")

# profil de chaque faux site : défauts SEO, liens cassés, mesures PageSpeed
SITES = {
    "demo-bon.ma": dict(meta_missing_every=0, h1_missing_every=0, long_title_every=9, no_alt_every=0, slow=0.3,
                        broken=0, https=True, robots=True, sitemap=True,
                        mobile=(88, 2.1, 90, 0.04), desktop=(96, 1.2, 40, 0.01), field_inp=("FAST", 120)),
    "demo-moyen.ma": dict(meta_missing_every=4, h1_missing_every=6, long_title_every=3, no_alt_every=5, slow=1.2,
                          broken=3, https=True, robots=True, sitemap=False,
                          mobile=(61, 3.6, 310, 0.14), desktop=(82, 1.9, 120, 0.05), field_inp=("AVERAGE", 260)),
    "demo-faible.ma": dict(meta_missing_every=2, h1_missing_every=3, long_title_every=2, no_alt_every=2, slow=4.0,
                           broken=9, https=False, robots=False, sitemap=False,
                           mobile=(32, 6.4, 780, 0.38), desktop=(55, 3.3, 350, 0.17), field_inp=None),
}


def make_pages(domain: str, p: dict) -> list:
    pages = []
    for i in range(20):
        no_meta = p["meta_missing_every"] and i % p["meta_missing_every"] == 0
        no_h1 = p["h1_missing_every"] and i % p["h1_missing_every"] == 0
        long_t = p["long_title_every"] and i % p["long_title_every"] == 0
        title = f"Page {i} de {domain} " + ("avec un titre vraiment beaucoup trop long pour Google " if long_t else "| Accueil")
        meta = None if no_meta else f"Description de la page {i} du site {domain}, avec assez de texte pour être utile aux visiteurs."
        pages.append({
            "url": f"https://www.{domain}/" + ("" if i == 0 else f"page-{i}/"), "status": 200,
            "redirects": 2 if i == 7 and p["broken"] else 0, "ttfb_s": round(p["slow"] + (i % 4) * 0.2, 2),
            "size_kb": 120.0 + i * 3, "title": title, "title_len": len(title),
            "meta_desc": meta, "meta_len": len(meta or ""), "h1_count": 0 if no_h1 else 1,
            "h1": [] if no_h1 else [f"Titre {i}"], "images_total": 12,
            "images_missing_alt": 4 if p["no_alt_every"] and i % p["no_alt_every"] == 0 else 0,
            "canonical": f"https://www.{domain}/page-{i}/", "viewport": True, "noindex": i in (18, 19),
            "word_count": 650, "_links": [],
        })
    return pages


def make_perf(domain: str, p: dict) -> dict:
    def block(vals, field):
        score, lcp, tbt, cls = vals
        rate = lambda v, good, ok: "bon" if v <= good else "à améliorer" if v <= ok else "mauvais"
        return {"available": True, "score": score, "rating": "bon" if score >= 90 else "à améliorer" if score >= 50 else "mauvais",
                "final_url": f"https://www.{domain}/",
                "lab": {"fcp_s": 1.6, "lcp_s": lcp, "tbt_ms": tbt, "cls": cls, "speed_index_s": 3.2, "tti_s": 4.5},
                "lab_rating": {"fcp_s": "bon", "lcp_s": rate(lcp, 2.5, 4.0), "tbt_ms": rate(tbt, 200, 600),
                               "cls": rate(cls, 0.1, 0.25), "speed_index_s": "bon", "tti_s": "bon"},
                "field": field,
                "google_opportunities": [
                    {"id": "unused-javascript", "title": "Réduire le JavaScript inutilisé", "savings_ms": int(tbt * 3),
                     "display_value": "Économies estimées", "description": "Réduisez le JavaScript inutilisé."},
                    {"id": "modern-image-formats", "title": "Servir des images au format moderne", "savings_ms": int(lcp * 250),
                     "display_value": "Économies estimées", "description": "Utilisez WebP ou AVIF."}],
                "accessibility": {"score": 90 - (100 - score) // 4, "rating": "à améliorer", "issues": [
                    {"id": "color-contrast", "title": "Contraste insuffisant", "display_value": None, "weight": 7}]},
                "best_practices": {"score": 96, "rating": "bon", "issues": []}, "error": None}

    inp = p["field_inp"]
    field = None if not inp else {"lcp_s": {"value": p["mobile"][1], "category": "AVERAGE"},
                                  "cls": {"value": p["mobile"][3], "category": "AVERAGE"},
                                  "inp_ms": {"value": inp[1], "category": inp[0]}, "fcp_s": None, "overall": "AVERAGE"}
    m, d = block(p["mobile"], field), block(p["desktop"], None)
    return {"url": f"https://www.{domain}", "domain": domain, "measured_at": "2026-10-25T14:30:00", "api_available": True,
            "mobile": m, "desktop": d,
            "summary": {"mobile_score": m["score"], "desktop_score": d["score"], "mobile_lcp_s": m["lab"]["lcp_s"],
                        "mobile_cls": m["lab"]["cls"], "mobile_tbt_ms": m["lab"]["tbt_ms"], "worst_rating": m["rating"]}}


def make_audit(domain: str, p: dict) -> dict:
    pages = make_pages(domain, p)
    broken = [{"url": f"https://www.{domain}/ancienne-page-{k}/", "status": 404, "category": "broken",
               "internal": True, "found_on": f"https://www.{domain}/page-{k + 1}/"} for k in range(p["broken"])]
    summary = build_summary(pages, broken)
    for pg in pages:
        pg.pop("_links", None)
    return {"url": f"https://www.{domain}/", "domain": domain, "audited_at": "2026-10-25T12:00:00",
            "access": {"reachable": True, "status": 200, "final_url": f"https://www.{domain}/", "response_time_s": 0.8, "error": None},
            "robots": {"exists": p["robots"], "status": 200, "blocks_all": False, "sitemaps": []},
            "sitemap": {"exists": p["sitemap"], "url": f"https://www.{domain}/sitemap.xml" if p["sitemap"] else None,
                        "url_count": 120 if p["sitemap"] else 0, "in_robots": False},
            "https": {"https_available": p["https"], "http_redirects_to_https": p["https"], "hsts": p["https"],
                      "cert_valid": True if p["https"] else None, "cert_days_left": 80 if p["https"] else None},
            "pages": pages, "broken_links": broken, "summary": summary}


if __name__ == "__main__":
    RESULTS.mkdir(exist_ok=True)
    if "--clean" in sys.argv:
        for f in RESULTS.glob("demo-*"):
            f.unlink()
        print("Fichiers de démonstration supprimés.")
        sys.exit()
    for domain, profile in SITES.items():
        (RESULTS / f"{domain}.json").write_text(json.dumps(make_audit(domain, profile), ensure_ascii=False, indent=2), encoding="utf-8")
        (RESULTS / f"{domain}_performance.json").write_text(json.dumps(make_perf(domain, profile), ensure_ascii=False, indent=2), encoding="utf-8")
    print("3 sites de démonstration créés dans results/. Lancez : streamlit run app.py")
