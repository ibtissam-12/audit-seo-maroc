"""Tests de l'adaptateur de données et du rapport PDF (src/report.py), sans Internet."""
import json

import demo_data  # génère de faux résultats au format des modules de l'équipe
import pytest

from src.report import (build_issues, comparison_rows, cwv_items, device, generate_pdf, load_sites)


@pytest.fixture()
def results_dir(tmp_path):
    """3 faux sites (bon, moyen, faible) avec audit + performance."""
    for domain, profile in demo_data.SITES.items():
        (tmp_path / f"{domain}.json").write_text(json.dumps(demo_data.make_audit(domain, profile)), encoding="utf-8")
        (tmp_path / f"{domain}_performance.json").write_text(json.dumps(demo_data.make_perf(domain, profile)), encoding="utf-8")
    return tmp_path


def test_load_sites_and_scores_are_ordered(results_dir):
    sites = {s["domain"]: s for s in load_sites(results_dir)}
    assert set(sites) == {"demo-bon.ma", "demo-moyen.ma", "demo-faible.ma"}
    score = {d: s["reco"]["health_score"] for d, s in sites.items()}
    assert score["demo-bon.ma"] > score["demo-moyen.ma"] > score["demo-faible.ma"]


def test_performance_file_is_optional(results_dir):
    (results_dir / "demo-bon.ma_performance.json").unlink()
    site = next(s for s in load_sites(results_dir) if s["domain"] == "demo-bon.ma")
    assert site["perf"] is None and device(site, "mobile") is None
    assert all(i["value"] == "N/D" for i in cwv_items(device(site, "mobile")))


def test_issues_are_sorted_and_valid(results_dir):
    site = next(s for s in load_sites(results_dir) if s["domain"] == "demo-faible.ma")
    issues = build_issues(site)
    assert issues and {i["category"] for i in issues} <= {"tags", "crawl", "images", "links"}
    order = {"Haute": 0, "Moyenne": 1, "Faible": 2}
    assert [order[i["severity"]] for i in issues] == sorted(order[i["severity"]] for i in issues)
    assert any("Lien cassé" in i["issue"] for i in issues)


def test_cwv_ratings(results_dir):
    site = next(s for s in load_sites(results_dir) if s["domain"] == "demo-faible.ma")
    items = {i["name"]: i for i in cwv_items(device(site, "mobile"))}
    assert items["LCP"]["rating"] == "mauvais" and items["INP"]["rating"] is None   # INP absent : pas de donnée terrain


def test_comparison_has_one_value_per_site(results_dir):
    sites = load_sites(results_dir)
    rows = comparison_rows(sites)
    assert rows and all(len(vals) == len(sites) for _, vals in rows)


def test_pdf_single_and_multi(results_dir, tmp_path):
    sites = load_sites(results_dir)
    one = generate_pdf(sites[:1], tmp_path / "un.pdf")
    many = generate_pdf(sites, tmp_path / "trois.pdf")
    assert one.read_bytes()[:4] == b"%PDF" and many.read_bytes()[:4] == b"%PDF"
    assert many.stat().st_size > one.stat().st_size      # le rapport comparatif est plus complet


def test_pdf_survives_missing_data(results_dir, tmp_path):
    (results_dir / "demo-moyen.ma_performance.json").unlink()                  # PageSpeed absent
    (results_dir / "site-ko.ma.json").write_text(json.dumps(                    # site injoignable
        {"url": "https://site-ko.ma", "domain": "site-ko.ma", "error": "Délai dépassé (timeout)"}), encoding="utf-8")
    out = generate_pdf(load_sites(results_dir), tmp_path / "mixte.pdf")
    assert out.read_bytes()[:4] == b"%PDF"


def test_pdf_refuses_empty_list(tmp_path):
    with pytest.raises(ValueError):
        generate_pdf([], tmp_path / "vide.pdf")
