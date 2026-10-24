"""Tests du module A sur des faux sites locaux (sans Internet)."""
from src.audit import _session, analyze_html, audit_site, check_access


def test_analyze_html():
    d = analyze_html('<html><head><title>T</title></head><body><h1>a</h1><img src="x"><img src="y" alt=""></body></html>',
                     "https://x.ma/")
    assert d["title"] == "T" and d["h1_count"] == 1
    assert d["images_missing_alt"] == 1          # alt="" accepté (décoratif)
    assert d["meta_desc"] is None


def test_audit_site_contract(fake_servers):
    r = audit_site("http://127.0.0.1:8102", max_pages=8, delay=0)
    for key in ("access", "robots", "sitemap", "https", "pages", "broken_links", "summary"):
        assert key in r
    assert r["summary"]["broken_links"] >= 1      # le lien /cassee
    assert r["summary"]["multiple_h1"] >= 1


def test_unreachable_site_does_not_crash():
    r = audit_site("http://127.0.0.1:9", max_pages=3, delay=0)   # port fermé
    assert "error" in r and r["access"]["reachable"] is False


def test_404_homepage_reported(fake_servers):
    a = check_access("http://127.0.0.1:8101/page-inexistante", _session())
    assert a["status"] == 404 and a["reachable"] is False


def test_robots_blocks_all_detected(fake_servers):
    r = audit_site("http://127.0.0.1:8103", max_pages=3, delay=0)
    assert r["robots"]["blocks_all"] is True
    assert r["https"]["https_available"] is False      # le faux site est en http


def test_link_classification():
    from src.audit import _classify
    assert _classify(404, True) == "broken" and _classify(500, False) == "broken"
    assert _classify(None, True) == "unreachable"      # timeout : pas un lien cassé prouvé
    assert _classify(403, True) == "blocked" and _classify(999, False) == "blocked"
    assert _classify(200, True) == "ok"


def test_broken_link_has_category(fake_servers):
    r = audit_site("http://127.0.0.1:8102", max_pages=8, delay=0)
    cassee = [b for b in r["broken_links"] if b["url"].endswith("/cassee")]
    assert cassee and cassee[0]["category"] == "broken"
    assert r["summary"]["unreachable_links"] == 0


def _page(**kw):
    base = {"url": "https://x.ma/", "status": 200, "title": "T" * 40, "title_len": 40, "meta_desc": "m" * 100,
            "meta_len": 100, "h1_count": 1, "images_missing_alt": 0, "noindex": False, "ttfb_s": 0.2}
    base.update(kw)
    return base


def test_summary_ignores_noindex_pages():
    from src.audit import build_summary
    pages = [_page(url="https://x.ma/a", title="A" * 40), _page(url="https://x.ma/b", title="B" * 40),
             # deux pages "aide" noindex avec le même title, sans meta ni H1 : ne doivent PAS compter
             _page(url="https://x.ma/aide1", title="Aide", title_len=4, meta_desc=None, meta_len=0, h1_count=0, noindex=True),
             _page(url="https://x.ma/aide2", title="Aide", title_len=4, meta_desc=None, meta_len=0, h1_count=0, noindex=True)]
    sm = build_summary(pages, [])
    assert sm["pages_indexable"] == 2 and sm["noindex_pages"] == 2
    assert sm["duplicate_title"] == 0 and sm["missing_h1"] == 0 and sm["missing_meta"] == 0