"""Admin quality pages backed by ClickHouse: stop-cell sample and thresholds (FR-QA-06, AC-16)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from clickhouse_connect.driver.client import Client
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from payintel.api.deps import AppState
from payintel.core.clock import FixedClock
from payintel.crawl.checkout.stops import StopContext, stop_row
from payintel.crawl.checkout.types import StepTiming, Stop, WalkStep
from payintel.history.writer import ObservationBuffer
from tests.stage3.conftest import World, login


def _write_stops(ch: Client, when: datetime, n: int) -> None:
    buf = ObservationBuffer(ch)
    for i in range(1, n + 1):
        run_id = uuid.uuid4()
        prefix = f"checkout/shop{i}.de/{run_id}/"
        ctx = StopContext(
            scan_run_id=run_id,
            host_id=i,
            etld1=f"shop{i}.de",
            platform_id="woocommerce",
            adapter="woocommerce",
            scanner_version="0.2.0",
            ruleset_version="r1",
            artifact_prefix=prefix,
        )
        stop = Stop(WalkStep.CHECKOUT, "checkout_not_found", "", page_url="https://x/checkout")
        buf.add(
            "obs_scan_stop",
            stop_row(
                stop,
                ctx,
                steps=[StepTiming("navigation", 100)],
                now=when - timedelta(minutes=i),
                artifact_key=f"{prefix}stop.jpg",
            ),
        )
    buf.flush()


def test_stop_cell_sample_page_and_csv(
    client: TestClient,
    db_session: Session,
    world: World,
    app_state: AppState,
    fixed_clock: FixedClock,
    ch_client: Client,
) -> None:
    app_state.ch = ch_client
    _write_stops(ch_client, fixed_clock.now(), 25)
    login(client, world.staff_analyst_email, state=app_state, session=db_session)
    page = client.get("/admin/quality")
    assert page.status_code == 200
    assert "/admin/quality/stops/checkout/checkout_not_found?" in page.text  # cells are links
    assert "All NFR-Q-06" in page.text or "NFR-Q-0" in page.text  # threshold block renders
    page = client.get("/admin/quality/stops/checkout/checkout_not_found?platform=woocommerce")
    assert page.status_code == 200
    assert page.text.count("stop.jpg") == 20 and page.text.count("stop.html.gz") == 20
    assert "shop1.de" in page.text and "shop25.de" not in page.text  # newest 20, distinct domains
    csv = client.get("/admin/quality/stops/checkout/checkout_not_found?csv=1")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert len(csv.text.strip().splitlines()) == 21
    assert client.get("/admin/quality/stops/cart/nothing").text.count("No stops") == 1


def test_stop_cell_sample_needs_staff(
    client: TestClient, db_session: Session, world: World, app_state: AppState
) -> None:
    login(client, world.org_admin_email, state=app_state, session=db_session)
    assert client.get("/admin/quality/stops/checkout/captcha").status_code == 403
