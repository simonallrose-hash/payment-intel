"""SQLAlchemy models for every PostgreSQL entity of section 5.1.

Importing this package registers all tables on `Base.metadata` (used by Alembic).
"""

from payintel.core.models.access import ApiKey, Membership, User
from payintel.core.models.alerts import AlertRule, Delivery, Watchlist, WatchlistItem, Webhook
from payintel.core.models.assets import HostJsAsset, JsAsset
from payintel.core.models.audit import AuditLog, FeatureFlag, OptoutRequest, UsageLog
from payintel.core.models.base import Base
from payintel.core.models.domains import Domain, DomainSource, Host, ImportBatch
from payintel.core.models.exports import Canary, ExportJob
from payintel.core.models.orgs import Contract, Entitlement, KycRecord, Organization
from payintel.core.models.portal import DsarRequest, PortalSession, ReportJob, SystemCursor
from payintel.core.models.quality import GoldLabel, QualityAlert
from payintel.core.models.reference import PaymentMethod, Platform, Provider, Vertical
from payintel.core.models.rules import DetectionRule
from payintel.core.models.scans import ScanPlan, ScanRun, StoreAccount
from payintel.core.models.store import (
    ChangeEvent,
    StoreCheckoutHost,
    StorePaymentMethod,
    StoreProfile,
    StoreProvider,
)

__all__ = [
    "AlertRule",
    "ApiKey",
    "AuditLog",
    "Base",
    "Canary",
    "ChangeEvent",
    "Contract",
    "Delivery",
    "DetectionRule",
    "Domain",
    "DomainSource",
    "DsarRequest",
    "Entitlement",
    "ExportJob",
    "FeatureFlag",
    "GoldLabel",
    "Host",
    "HostJsAsset",
    "ImportBatch",
    "JsAsset",
    "KycRecord",
    "Membership",
    "OptoutRequest",
    "Organization",
    "PaymentMethod",
    "Platform",
    "PortalSession",
    "Provider",
    "QualityAlert",
    "ReportJob",
    "ScanPlan",
    "ScanRun",
    "StoreAccount",
    "StoreCheckoutHost",
    "StorePaymentMethod",
    "StoreProfile",
    "StoreProvider",
    "SystemCursor",
    "UsageLog",
    "User",
    "Vertical",
    "Watchlist",
    "WatchlistItem",
    "Webhook",
]
