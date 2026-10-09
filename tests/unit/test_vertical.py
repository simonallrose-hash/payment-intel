"""FR-DT-10: vertical from menu categories, schema.org and meta/text keywords."""

from __future__ import annotations

from payintel.core.models.base import ConfidenceLevel
from payintel.crawl.light.html import extract
from payintel.detect.vertical import VerticalDetector

FASHION_HOME = """<!doctype html><html lang="de"><head>
<title>Beispiel Laden – Mode aus Berlin</title>
<meta name="description" content="Kleidung, Schuhe und Accessoires für Damen und Herren">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList",
"itemListElement":[{"@type":"ListItem","position":1,"name":"Damenmode"},
{"@type":"ListItem","position":2,"name":"Kleider"}]}</script></head>
<body><nav><a href="/collections/damenmode">Damenmode</a>
<a href="/collections/herrenmode">Herren</a>
<a href="/collections/schuhe">Schuhe</a><a href="/collections/jeans">Jeans</a>
<a href="/cart">Warenkorb</a>
<a href="https://instagram.com/x">Insta</a></nav>
<main>Neue Mode für den Herbst: Jacken, Hosen und Schuhe.</main></body></html>"""

PET_PRODUCT = """<html><head><title>Hundefutter Premium 12 kg</title>
<script type="application/ld+json">{"@type":"Product","name":"Hundefutter Premium",
"category":"Tierbedarf > Hund > Futter"}</script>
</head><body><a href="/tierbedarf/hund">Hund</a><a href="/tierbedarf/katze">Katze</a>
<p>Futter für Hund und Katze, Zubehör für Haustiere.</p></body></html>"""

PLAIN = (
    "<html><head><title>Welcome</title></head>"
    "<body><a href='/about'>About us</a><p>Hello.</p></body></html>"
)


def test_fashion_store_is_high_confidence() -> None:
    det = VerticalDetector()
    r = det.detect(extract(FASHION_HOME, "https://woo-shop.test/"))
    assert r.vertical_id == "fashion" and r.confidence == ConfidenceLevel.HIGH
    assert r.votes["fashion"] >= 6 and any(s.startswith("menu:fashion") for s in r.signals)
    assert any(s.startswith("schema:fashion") for s in r.signals)
    # the social link is external and does not count as a menu entry
    assert "instagram" not in det.menu_text(extract(FASHION_HOME, "https://woo-shop.test/"))


def test_product_page_adds_schema_category() -> None:
    det = VerticalDetector()
    r = det.detect(
        extract(PLAIN, "https://pets.test/"), extract(PET_PRODUCT, "https://pets.test/p/1")
    )
    assert r.vertical_id == "pets" and r.confidence in (
        ConfidenceLevel.MEDIUM,
        ConfidenceLevel.HIGH,
    )
    assert r.votes["pets"] == max(r.votes.values())


def test_no_keywords_means_unknown_not_other() -> None:
    r = VerticalDetector().detect(extract(PLAIN, "https://plain.test/"))
    assert r.vertical_id is None and r.confidence is None and r.votes == {}


def test_keyword_match_is_word_bounded_and_multilingual() -> None:
    det = VerticalDetector()
    assert "other" not in det.keywords
    # "art" must not fire inside "part"; Polish diacritics and French phrases match
    r = det.detect(extract("<html><body><p>spare part</p></body></html>", "https://x.test/"))
    assert "art_crafts" not in r.votes
    r = det.detect(
        extract(
            "<html><head><title>Biżuteria i zegarki — loisirs créatifs</title></head>"
            "<body><a href='/bizuteria/pierscionki'>x</a></body></html>",
            "https://x.test/",
        )
    )
    assert r.votes.get("jewelry_watches", 0) > r.votes.get("art_crafts", 0) > 0
