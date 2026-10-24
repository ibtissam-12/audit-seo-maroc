"""Fixture pytest : les faux sites sont démarrés UNE SEULE FOIS pour toute la session de tests."""
import fake_sites
import pytest


@pytest.fixture(scope="session")
def fake_servers():
    servers = fake_sites.start_all()
    yield servers
    fake_sites.stop_all(servers)