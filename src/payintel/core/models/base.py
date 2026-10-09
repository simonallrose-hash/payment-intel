"""Declarative base, shared column types and the closed vocabularies of 5.1/5.3."""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated

from sqlalchemy import DateTime, MetaData, String, text
from sqlalchemy.orm import DeclarativeBase, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Reusable annotated types
Slug = Annotated[str, mapped_column(String(64))]
TimestampTZ = Annotated[datetime, mapped_column(DateTime(timezone=True))]
CreatedAt = Annotated[
    datetime,
    mapped_column(DateTime(timezone=True), server_default=text("now()"), nullable=False),
]


class StrEnum(str, enum.Enum):
    """String enum stored as VARCHAR + CHECK (non-native, see `enum_column`)."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


# --- 5.1 domain / host ------------------------------------------------------
class DomainStatus(StrEnum):
    CANDIDATE = "candidate"
    ECOMMERCE = "ecommerce"
    NOT_ECOMMERCE = "not_ecommerce"
    PARKED = "parked"
    NO_DNS = "no_dns"
    UNREACHABLE = "unreachable"
    BLOCKED = "blocked"
    OPTOUT = "optout"


class DomainSourceKind(StrEnum):
    TRANCO = "tranco"
    COMMONCRAWL = "commoncrawl"
    CT = "ct"
    CRAWL_LINK = "crawl_link"
    MANUAL = "manual"
    CZDS = "czds"  # AS-22: never surfaced in C1 when it is the only source


# --- 4.4 / 5.1 scans --------------------------------------------------------
class ScanType(StrEnum):
    LIGHT = "light"
    CHECKOUT = "checkout"


class ScanStatus(StrEnum):
    """Final statuses of a run (FR-CW-09) plus light-scan outcomes."""

    REACHED_PAYMENT_STEP = "reached_payment_step"
    REACHED_CHECKOUT = "reached_checkout"
    REACHED_CART = "reached_cart"
    LOGIN_REQUIRED = "login_required"
    REGISTRATION_PENDING_VERIFICATION = "registration_pending_verification"
    NO_PRODUCT_FOUND = "no_product_found"
    ADD_TO_CART_FAILED = "add_to_cart_failed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    ERROR = "error"
    # light scan
    OK = "ok"
    HTTP_ERROR = "http_error"
    ROBOTS_DISALLOWED = "robots_disallowed"
    RUNNING = "running"


class Coverage(StrEnum):
    HOMEPAGE = "homepage"
    PRODUCT = "product"
    CART = "cart"
    CHECKOUT = "checkout"
    PAYMENT_STEP = "payment_step"


class StoreAccountStatus(StrEnum):
    ACTIVE = "active"
    PENDING_VERIFICATION = "pending_verification"
    FAILED = "failed"
    DELETED = "deleted"


# --- 5.3 classifiers --------------------------------------------------------
class ProviderRole(StrEnum):
    GATEWAY = "gateway"
    WALLET = "wallet"
    BNPL = "bnpl"
    LOCAL_METHOD_PROVIDER = "local_method_provider"
    ORCHESTRATOR = "orchestrator"
    FRAUD_TOOL = "fraud_tool"
    THREEDS_SDK = "3ds_sdk"


class PaymentMethodType(StrEnum):
    CARD = "card"
    WALLET = "wallet"
    BNPL = "bnpl"
    BANK_REDIRECT = "bank_redirect"
    BANK_TRANSFER = "bank_transfer"
    DIRECT_DEBIT = "direct_debit"
    CASH_VOUCHER = "cash_voucher"
    REAL_TIME_PAYMENT = "real_time_payment"
    CRYPTO = "crypto"
    COD = "cod"
    OTHER = "other"


class ReferenceStatus(StrEnum):
    ACTIVE = "active"
    ACQUIRED = "acquired"
    CLOSED = "closed"
    DEPRECATED = "deprecated"  # FR-NR-04: deletion is replaced by this mark


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SignalType(StrEnum):
    """FR-DT-02."""

    SCRIPT_SRC = "script_src"
    NETWORK_HOST = "network_host"
    IFRAME_SRC = "iframe_src"
    JS_GLOBAL = "js_global"
    HTML_PATTERN = "html_pattern"
    FORM_ACTION = "form_action"
    CHECKOUT_LABEL = "checkout_label"
    PLATFORM_PLUGIN = "platform_plugin"
    HEADER = "header"
    COOKIE = "cookie"
    FAVICON = "favicon"


class PageScope(StrEnum):
    """Where a rule may fire; feeds FR-DT-04 / FR-DT-05 scoring."""

    ANY = "any"
    HOMEPAGE = "homepage"
    PRODUCT = "product"
    CART = "cart"
    CHECKOUT = "checkout"


class RuleTargetType(StrEnum):
    PROVIDER = "provider"
    PAYMENT_METHOD = "payment_method"
    PLATFORM = "platform"
    TECH = "tech"


class ChangeEventType(StrEnum):
    """FR-HI-03."""

    PROVIDER_ADDED = "provider_added"
    PROVIDER_REMOVED = "provider_removed"
    METHOD_ADDED = "method_added"
    METHOD_REMOVED = "method_removed"
    PLATFORM_CHANGED = "platform_changed"
    CHECKOUT_HOST_ADDED = "checkout_host_added"
    CHECKOUT_HOST_REMOVED = "checkout_host_removed"
    STORE_OFFLINE = "store_offline"
    STORE_ONLINE = "store_online"


class GoldEntityType(StrEnum):
    PROVIDER = "provider"
    PAYMENT_METHOD = "payment_method"
    PLATFORM = "platform"
    COUNTRY = "country"


# --- 4.14 organisations -----------------------------------------------------
class OrgStatus(StrEnum):
    """FR-KYC-01 lifecycle."""

    APPLIED = "applied"
    KYC_IN_PROGRESS = "kyc_in_progress"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    TERMINATED = "terminated"


class Product(StrEnum):
    C_REPORT = "c_report"
    C_DATA = "c_data"
    C2 = "c2"


class FieldProfile(StrEnum):
    """FR-KYC-05; `c2_risk` requires `feature_c2_enabled` (FR-KYC-07)."""

    C1_BASIC = "c1_basic"
    C1_FULL = "c1_full"
    C2_RISK = "c2_risk"


class Role(StrEnum):
    """3.2."""

    ORG_VIEWER = "org_viewer"
    ORG_ANALYST = "org_analyst"
    ORG_ADMIN = "org_admin"
    API_CLIENT = "api_client"
    STAFF_SUPPORT = "staff_support"
    STAFF_ANALYST = "staff_analyst"
    STAFF_COMPLIANCE = "staff_compliance"
    STAFF_ADMIN = "staff_admin"


class ExportType(StrEnum):
    """FR-EX-02."""

    FULL_SNAPSHOT = "full_snapshot"
    INCREMENT = "increment"
    MARKET_AGGREGATES = "market_aggregates"


class ExportFormat(StrEnum):
    PARQUET = "parquet"
    CSV = "csv"


class ExportStatus(StrEnum):
    PENDING = "pending"
    AWAITING_APPROVAL = "awaiting_approval"  # FR-EX-06
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    EXPIRED = "expired"


class DeliveryStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class OptoutMethod(StrEnum):
    """FR-OO-02."""

    DNS_TXT = "dns_txt"
    WELL_KNOWN = "well_known"


class KycDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class DsarKind(StrEnum):
    """FR-OO-03."""

    ACCESS = "access"
    ERASURE = "erasure"


class DsarStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class ReportStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


def enum_values(e: type[StrEnum]) -> list[str]:
    return [m.value for m in e]
