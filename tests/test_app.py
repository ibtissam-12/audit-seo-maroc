"""Tests de l'interface Streamlit (app.py) avec le moteur de test intégré de Streamlit, sans navigateur."""
import json
from pathlib import Path

import demo_data
import pytest
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parent.parent / "app.py"
PAGES = ["page_dashboard", "page_seo", "page_perf", "page_reco", "page_compare", "page_pdf"]


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """Dossier de travail temporaire avec results/ rempli de 3 faux sites."""
    results = tmp_path / "results"
    results.mkdir()
    for domain, profile in demo_data.SITES.items():
        (results / f"{domain}.json").write_text(json.dumps(demo_data.make_audit(domain, profile)), encoding="utf-8")
        (results / f"{domain}_performance.json").write_text(json.dumps(demo_data.make_perf(domain, profile)), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run_page(page_function: str) -> AppTest:
    """Exécute une page précise (on remplace la navigation par l'appel direct de la page)."""
    return AppTest.from_string(APP.read_text(encoding="utf-8").replace("nav.run()", f"{page_function}()"),
                               default_timeout=120).run()


@pytest.mark.parametrize("page", PAGES)
def test_every_page_loads(project, page):
    assert not run_page(page).exception


def test_default_page_loads(project):
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception


def test_every_page_loads_for_each_site(project):
    for site in demo_data.SITES:
        at = AppTest.from_file(str(APP), default_timeout=120).run()
        at.sidebar.selectbox[0].set_value(site).run()
        assert not at.exception, site


def test_empty_results_show_a_message(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    assert not at.exception and at.info


def test_site_report_download(project):
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    next(b for b in at.button if b.label == "Générer le rapport PDF").click().run()
    assert not at.exception and len(at.get("download_button")) == 1
    assert list((project / "reports").glob("rapport_*.pdf"))


def test_comparison_needs_two_sites(project):
    at = run_page("page_compare")
    at.multiselect[0].set_value(["demo-bon.ma"]).run()
    assert not at.exception and at.info
    at.multiselect[0].set_value(["demo-bon.ma", "demo-faible.ma"]).run()
    next(b for b in at.button if "comparatif" in b.label).click().run()
    assert not at.exception and list((project / "reports").glob("rapport_comparatif_*.pdf"))


def test_report_filename_is_valid_on_windows(project):
    """Un site avec port (127.0.0.1:8101) ne doit pas produire de « : » dans le nom du PDF."""
    (project / "results" / "127.0.0.1_8101.json").write_text(
        json.dumps(demo_data.make_audit("127.0.0.1:8101", demo_data.SITES["demo-moyen.ma"])), encoding="utf-8")
    at = AppTest.from_file(str(APP), default_timeout=120).run()
    at.sidebar.selectbox[0].set_value("127.0.0.1:8101").run()
    next(b for b in at.button if b.label == "Générer le rapport PDF").click().run()
    names = [p.name for p in (project / "reports").glob("*.pdf")]
    assert names and not any(":" in n for n in names)
