"""FR-RP-04 PDF output with charts and methodology; FR-RP-05 public summary without domains."""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.core.errors import ConflictError
from payintel.core.models.portal import ReportJob
from payintel.core.settings import Settings
from payintel.reports import aggregates, pdf, public
from payintel.reports import service as reports
from tests.stage3.conftest import World, login, staff_principal
from tests.stage3.test_reports import _seed_market

pytestmark = pytest.mark.integration


def _text(data: bytes) -> str:
    assert data.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() for page in reader.pages)


def test_pdf_and_public_summary_from_the_same_aggregates(
    db_session: Session, world: World, fixed_clock: FixedClock
) -> None:
    _seed_market(db_session, fixed_clock)
    spec = aggregates.ReportSpec(countries=("DE",), min_cell=30)
    report = aggregates.build(db_session, spec, now=fixed_clock.now())
    full = pdf.write_pdf(report, methodology_url="https://payintel.example/methodology")
    text = _text(full)
    assert "PayIntel C Report" in text and "Methodology" in text
    assert "DE|shopify" in text and "adyen" in text and "Wilson" in text
    assert "de-shop-0.de" not in text and "alpha-shop.de" not in text
    assert PdfReader(io.BytesIO(full)).pages.__len__() >= 3

    summary = public.from_report(report)
    assert [c.cell for c in summary.cells] == ["DE|shopify"]
    cell = summary.cells[0]
    assert cell.stores == 37 and [r.key for r in cell.psp][:1] == ["adyen"]
    assert any(r.key == "other" for r in cell.psp) and all(
        r.stores >= 30 or r.key == "other" for r in cell.psp
    )
    assert cell.bnpl_share is None  # 18 BNPL stores < floor of 30 → merged into 'other'
    assert cell.providers_per_store is not None and cell.providers_per_store > 1
    assert (
        summary.cells_withheld == report.suppressed_cells and summary.stores == report.total_stores
    )
    round_trip = public.PublicSummary.from_dict(summary.as_dict())
    assert round_trip.as_dict() == summary.as_dict()  # values rounded on the way out
    short = _text(pdf.write_public_pdf(summary, methodology_url="https://payintel.example/m"))
    assert summary.title in short and "Top payment providers" in short
    assert "de-shop" not in short and "DE|shopify" in short and "Methodology (short)" in short
    assert "PSP flows" not in short


def test_admin_builds_pdf_and_publishes_summary(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
) -> None:
    _seed_market(db_session, fixed_clock)
    csrf = login(client, world.staff_analyst_email, state=app_state, session=db_session)
    r = client.post(
        "/admin/reports",
        data={"csrf": csrf, "countries": "DE", "org_id": str(world.org.id)},
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    job = reports.recent(db_session, limit=1)[0]
    assert job.pdf_key == f"reports/{job.id}.pdf" and job.public_summary
    assert job.published_at is None and job.public_slug is None
    r = client.get(f"/admin/reports/{job.id}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "PayIntel C Report" in _text(r.content)
    assert client.get(f"/admin/reports/{job.id}/docx").status_code == 404
    page = client.get("/admin/reports")
    assert "Publish summary" in page.text and "PDF</a>" in page.text
    # nothing public yet
    assert client.get("/reports").status_code == 200
    assert "No summaries published yet" in client.get("/reports").text
    r = client.post(f"/admin/reports/{job.id}/publish", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303 and "Summary+published" in r.headers["location"]
    db_session.expire_all()
    job = reports.get_job(db_session, job.id)
    assert job.public_slug == f"de-{job.created_at:%Y-%m}-{job.id.hex[:8]}"
    assert job.public_pdf_key == f"reports/{job.id}-public.pdf"
    client.cookies.clear()  # anonymous visitor
    listing = client.get("/reports")
    assert (
        job.public_slug in listing.text
        and "Payment infrastructure of online stores: DE" in listing.text
    )
    page = client.get(f"/reports/{job.public_slug}")
    assert page.status_code == 200 and "DE|shopify" in page.text and "adyen" in page.text
    assert "de-shop" not in page.text and "alpha-shop" not in page.text
    r = client.get(f"/reports/{job.public_slug}.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "Top payment providers" in _text(r.content)
    assert client.get("/reports/nope").status_code == 404
    # the organisation the report was delivered to downloads the full PDF
    login(client, world.org_admin_email, state=app_state, session=db_session)
    r = client.get(f"/portal/reports/{job.id}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "PDF</a>" in client.get("/portal/reports").text
    client.cookies.clear()
    csrf = login(client, world.staff_analyst_email, state=app_state, session=db_session)
    r = client.post(
        f"/admin/reports/{job.id}/unpublish", data={"csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303
    client.cookies.clear()
    assert client.get(f"/reports/{job.public_slug}").status_code == 404
    assert client.get(f"/reports/{job.public_slug}.pdf").status_code == 404
    assert "No summaries published yet" in client.get("/reports").text


def test_publish_requires_a_finished_report(db_session: Session, fixed_clock: FixedClock) -> None:
    job = ReportJob(
        id=uuid.uuid4(), spec={"countries": ["DE"]}, requested_by="x", created_at=fixed_clock.now()
    )
    db_session.add(job)
    db_session.flush()
    with pytest.raises(ConflictError):
        reports.publish(
            db_session,
            job,
            principal=staff_principal(),
            store=None,
            settings=Settings(),
            clock=fixed_clock,
        )
    with pytest.raises(ConflictError):
        reports.unpublish(db_session, job, principal=staff_principal(), clock=fixed_clock)
