"""Mini-serveurs locaux simulant 3 sites de qualité SEO différente (tests sans Internet)."""
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

GOOD = """<html lang="fr"><head><title>Boutique Atlas - Artisanat marocain authentique en ligne</title>
<meta name="description" content="Découvrez notre sélection d'artisanat marocain : tapis, poterie et lampes faits main, livrés partout au Maroc.">
<meta name="viewport" content="width=device-width, initial-scale=1"><link rel="canonical" href="http://x/">
<meta property="og:title" content="Atlas"><script type="application/ld+json">{"@type":"Organization"}</script></head>
<body><h1>Artisanat marocain</h1><img src="a.jpg" alt="tapis"><a href="/p1">p1</a><a href="/p2">p2</a>
<p>%s</p></body></html>""" % ("mot " * 200)

MEDIUM = """<html lang="fr"><head><title>Accueil</title><meta name="viewport" content="width=device-width">
</head><body><h1>Bienvenue</h1><h1>Encore</h1><img src="a.jpg"><img src="b.jpg" alt="b">
<a href="/p1">p1</a><a href="/cassee">cassée</a><a href="/p2">p2</a><p>%s</p></body></html>""" % ("mot " * 160)

BAD = """<html><head></head><body><div>Page sans rien</div><img src="a.jpg"><img src="b.jpg">
<a href="/p1">p1</a><a href="/404a">x</a><a href="/404b">y</a></body></html>"""

SITES = {
    8101: {"/": GOOD, "/p1": GOOD.replace("Boutique Atlas", "Page 1 Boutique Atlas"), "/p2": GOOD.replace("Boutique Atlas", "Page 2 Boutique"),
           "/robots.txt": "User-agent: *\nAllow: /\nSitemap: http://127.0.0.1:8101/sitemap.xml",
           "/sitemap.xml": '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>http://127.0.0.1:8101/p1</loc></url></urlset>'},
    8102: {"/": MEDIUM, "/p1": MEDIUM, "/p2": MEDIUM, "/robots.txt": "User-agent: *\nDisallow: /admin"},
    8103: {"/": BAD, "/p1": BAD, "/robots.txt": "User-agent: *\nDisallow: /"},
}


class _Server(ThreadingHTTPServer):
    # Sous Windows, SO_REUSEADDR permet à deux serveurs de partager un même port :
    # un ancien serveur arrêté "avale" alors les connexions. On le désactive sous Windows.
    allow_reuse_address = (os.name != "nt")
    daemon_threads = True


def _handler(pages):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages.get(self.path)
            if body is None:
                self.send_response(404); self.end_headers(); return
            ctype = "text/html" if self.path not in ("/robots.txt",) and not self.path.endswith(".xml") else "text/plain"
            self.send_response(200); self.send_header("Content-Type", ctype + "; charset=utf-8"); self.end_headers()
            self.wfile.write(body.encode())
        do_HEAD = do_GET
        def log_message(self, *a): pass
    return H


def start_all():
    servers = []
    for port, pages in SITES.items():
        srv = _Server(("127.0.0.1", port), _handler(pages))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    return servers


def stop_all(servers):
    """Arrête proprement les serveurs ET libère les ports (server_close)."""
    for srv in servers:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    start_all()
    print("Sites de test : http://127.0.0.1:8101  8102  8103  (Ctrl+C pour arrêter)")
    threading.Event().wait()