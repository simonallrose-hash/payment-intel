"""Pure helpers of stage 3: entitlements, cursors, canaries, watermark, webhook
signatures, API-key parsing, CSRF, lockout, TOTP and rule-draft validation."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pyotp
import pytest

from payintel.alerts import webhook
from payintel.api.auth import csrf, keys, passwords, totp
from payintel.api.read import decode_cursor, encode_cursor
from payintel.core.crypto import SecretBox
from payintel.core.errors import ValidationError
from payintel.core.models.access import User
from payintel.core.models.base import FieldProfile, PageScope, Role, RuleTargetType, SignalType
from payintel.detect.admin import RuleDraft, validate
from payintel.entitlements.check import EntitlementDenied, ip_allowed, require_scope
from payintel.entitlements.model import Grant, Principal
from payintel.entitlements.segment import countries_in_segment, in_segment
from payintel.exports.canary import canary_count
from payintel.exports.watermark import order_matches, order_rows

ORG = uuid.uuid4()


def _grant(countries: set[str] | None = None, platforms: set[str] | None = None) -> Grant:
    countries = {"DE"} if countries is None else countries
    platforms = set() if platforms is None else platforms
    return Grant(
        org_id=ORG,
        contract_id=uuid.uuid4(),
        product="c1",
        profile=FieldProfile.C1_BASIC,
        countries=frozenset(countries),
        platforms=frozenset(platforms),
        api_rps=10,
        daily_records=50_000,
        monthly_records=1_000_000,
        export_max_rows=10_000,
        export_schedule="weekly",
        watchlist_limit=100,
        allowed_ips=(),
        contract_ends_on=date(2027, 1, 1),
    )


# --- entitlements ---------------------------------------------------------------


def test_ip_allowed_empty_list_means_unrestricted() -> None:
    assert ip_allowed("203.0.113.9", [])
    assert ip_allowed(None, [])


def test_ip_allowed_matches_addresses_and_networks() -> None:
    allowed = ["203.0.113.9", "198.51.100.0/24", "garbage"]
    assert ip_allowed("203.0.113.9", allowed)
    assert ip_allowed("198.51.100.77", allowed)
    assert not ip_allowed("192.0.2.1", allowed)
    assert not ip_allowed(None, allowed)
    assert not ip_allowed("not-an-ip", allowed)


def test_in_segment_country_and_platform() -> None:
    g = _grant({"DE"}, {"shopify"})
    assert in_segment(g, country="de", platform_id="shopify")
    assert not in_segment(g, country="DE", platform_id="magento2")
    assert not in_segment(g, country="FR", platform_id="shopify")
    assert in_segment(_grant(set(), set()), country=None, platform_id=None)


def test_countries_in_segment_defaults_and_rejects_outside() -> None:
    g = _grant({"DE", "AT"})
    assert countries_in_segment(g, []) == ["AT", "DE"]
    assert countries_in_segment(g, ["de"]) == ["DE"]
    with pytest.raises(EntitlementDenied) as exc:
        countries_in_segment(g, ["FR"])
    assert exc.value.reason == "outside_segment"


def test_require_scope_uses_denial_code() -> None:
    p = Principal(kind="api_key", org_id=ORG, role=Role.ORG_ADMIN, scopes=frozenset({"read"}))
    require_scope(p, "read")
    with pytest.raises(EntitlementDenied) as exc:
        require_scope(p, "export")
    assert exc.value.reason == "scope_missing"


# --- cursors --------------------------------------------------------------------


def test_cursor_round_trip_and_sort_mismatch() -> None:
    cur = encode_cursor("domain", "beta-store.de", 42)
    assert "=" not in cur
    assert decode_cursor(cur, "domain") == ("beta-store.de", 42)
    with pytest.raises(ValidationError):
        decode_cursor(cur, "traffic_rank")
    with pytest.raises(ValidationError):
        decode_cursor("not-base64!!", "domain")


# --- exports: canaries and watermark -------------------------------------------


def test_canary_count_is_bounded_and_deterministic() -> None:
    seen = set()
    for _ in range(200):
        eid = uuid.uuid4()
        n = canary_count(eid, low=3, high=10)
        assert 3 <= n <= 10
        assert n == canary_count(eid, low=3, high=10)
        seen.add(n)
    assert len(seen) > 3


def test_watermark_order_is_unique_per_export_and_verifiable() -> None:
    rows = [{"domain": f"shop{i}.de"} for i in range(50)]
    w1, w2 = uuid.uuid4(), uuid.uuid4()
    o1 = order_rows(rows, w1, lambda r: r["domain"])
    o2 = order_rows(rows, w2, lambda r: r["domain"])
    assert o1 != o2
    assert o1 == order_rows(rows, w1, lambda r: r["domain"])
    assert order_matches(o1, w1, lambda r: r["domain"])
    assert not order_matches(o1, w2, lambda r: r["domain"])


# --- webhook signatures ---------------------------------------------------------


def test_webhook_signature_verifies_within_tolerance() -> None:
    secret = webhook.new_secret()
    body = b'{"event":"provider_added"}'
    header = webhook.sign(secret, body, ts=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert webhook.verify(secret, body, header, now_ts=1_700_000_100)
    assert not webhook.verify(secret, body, header, now_ts=1_700_001_000)
    assert not webhook.verify(secret, body + b" ", header, now_ts=1_700_000_000)
    assert not webhook.verify("other", body, header, now_ts=1_700_000_000)
    assert not webhook.verify(secret, body, "t=abc,v1=00", now_ts=1_700_000_000)


# --- API keys, CSRF, lockout ----------------------------------------------------


def test_parse_prefix_accepts_only_well_formed_keys() -> None:
    secret = "x" * 43
    assert keys.parse_prefix(f"pik_abcdefgh.{secret}", "pik_") == "pik_abcdefgh"
    assert keys.parse_prefix(f"pik_abc.{secret}", "pik_") is None
    assert keys.parse_prefix("pik_abcdefgh.short", "pik_") is None
    assert keys.parse_prefix(f"sk_abcdefgh.{secret}", "pik_") is None
    assert keys.parse_prefix("pik_abcdefgh", "pik_") is None


def test_csrf_check() -> None:
    csrf.check("token", "token")
    with pytest.raises(csrf.CsrfError):
        csrf.check("token", "other")
    with pytest.raises(csrf.CsrfError):
        csrf.check("token", None)


def test_lockout_after_max_attempts() -> None:
    now = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    user = User(email="x@example.test", password_hash="h", failed_logins=0, locked_until=None)
    for _ in range(9):
        passwords.register_failure(user, now=now, max_attempts=10, window_minutes=15)
        assert not passwords.is_locked(user, now)
    passwords.register_failure(user, now=now, max_attempts=10, window_minutes=15)
    assert passwords.is_locked(user, now)
    assert not passwords.is_locked(user, now + timedelta(minutes=15, seconds=1))
    passwords.register_success(user, now=now)
    assert user.locked_until is None and user.failed_logins == 0


def test_password_hash_is_argon2id() -> None:
    h = passwords.hash_password("correct horse")
    assert h.startswith("$argon2id$")
    assert passwords.verify_password(h, "correct horse")
    assert not passwords.verify_password(h, "wrong")


def test_totp_secret_bound_to_user_id() -> None:
    box = SecretBox(bytes(range(32)))
    user = User(id=uuid.uuid4(), email="t@example.test", password_hash="h")
    secret = totp.new_secret()
    totp.store_secret(user, secret, box)
    assert secret not in (user.totp_secret_encrypted or "")
    assert not totp.enrolled(user)
    user.totp_confirmed_at = datetime(2026, 10, 9, tzinfo=UTC)
    assert totp.enrolled(user)
    at = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
    assert totp.verify_code(user, pyotp.TOTP(secret).at(at), box, at=at)
    assert not totp.verify_code(user, "000000", box, at=at)
    other = User(id=uuid.uuid4(), email="o@example.test", password_hash="h")
    other.totp_secret_encrypted = user.totp_secret_encrypted
    with pytest.raises(Exception):  # noqa: B017 - AES-GCM tag mismatch, wrapped by cryptography
        totp.verify_code(other, "000000", box, at=at)


# --- admin rule drafts ----------------------------------------------------------


def _draft(**over: object) -> RuleDraft:
    base: dict[str, object] = {
        "rule_id": "stripe.custom.1",
        "target_type": RuleTargetType.PROVIDER,
        "target_id": "stripe",
        "signal_type": SignalType.NETWORK_HOST,
        "pattern": "*.stripe.com",
        "weight": 0.6,
        "page_scope": PageScope.CHECKOUT,
    }
    base.update(over)
    return RuleDraft(**base)  # type: ignore[arg-type]


def test_rule_draft_validation() -> None:
    validate(_draft())
    with pytest.raises(ValidationError):
        validate(_draft(rule_id="has space"))
    with pytest.raises(ValidationError):
        validate(_draft(weight=1.5))
    with pytest.raises(ValidationError):
        validate(_draft(match="fuzzy"))
    with pytest.raises(ValidationError):
        validate(_draft(signal_type=SignalType.HTML_PATTERN, pattern="(unclosed"))
    rule = _draft().as_rule(3)
    assert rule.version == 3 and rule.match == "host_suffix" and rule.source_file == "admin"
