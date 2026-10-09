"""CRUD `/v1/webhooks` (FR-AL-04). The secret is returned once, on creation."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from payintel.alerts import webhook as wh
from payintel.api.deps import Access, SessionDep, StateDep, scoped
from payintel.api.schemas.common import WebhookCreated, WebhookIn, WebhookOut, WebhookUpdate

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])
ReadAccess = Annotated[Access, Depends(scoped("stores:read"))]
WriteAccess = Annotated[Access, Depends(scoped("webhooks:write"))]


def _out(h: Any) -> WebhookOut:
    return WebhookOut(id=h.id, url=h.url, enabled=h.enabled, created_at=h.created_at)


@router.get("", response_model=list[WebhookOut])
def list_webhooks(request: Request, session: SessionDep, access: ReadAccess) -> Any:
    request.state.records = 0
    return [_out(h) for h in wh.list_webhooks(session, access.org_id)]


@router.post("", response_model=WebhookCreated, status_code=201)
def create_webhook(
    body: WebhookIn, request: Request, state: StateDep, session: SessionDep, access: WriteAccess
) -> Any:
    hook, secret = wh.create_webhook(
        session,
        org_id=access.org_id,
        url=body.url,
        enabled=body.enabled,
        box=state.secret_box,
        actor=access.principal.actor,
        clock=state.clock,
    )
    request.state.records = 0
    return WebhookCreated(
        id=hook.id, url=hook.url, enabled=hook.enabled, created_at=hook.created_at, secret=secret
    )


@router.get("/{webhook_id}", response_model=WebhookOut)
def get_webhook(webhook_id: int, request: Request, session: SessionDep, access: ReadAccess) -> Any:
    request.state.records = 0
    return _out(wh.get_webhook(session, access.org_id, webhook_id))


@router.patch("/{webhook_id}", response_model=WebhookOut)
def update_webhook(
    webhook_id: int,
    body: WebhookUpdate,
    request: Request,
    state: StateDep,
    session: SessionDep,
    access: WriteAccess,
) -> Any:
    hook = wh.get_webhook(session, access.org_id, webhook_id)
    request.state.records = 0
    return _out(
        wh.update_webhook(
            session,
            hook,
            url=body.url,
            enabled=body.enabled,
            box=state.secret_box,
            actor=access.principal.actor,
            clock=state.clock,
        )
    )


@router.delete("/{webhook_id}", status_code=204)
def delete_webhook(
    webhook_id: int, request: Request, state: StateDep, session: SessionDep, access: WriteAccess
) -> None:
    hook = wh.get_webhook(session, access.org_id, webhook_id)
    wh.delete_webhook(session, hook, actor=access.principal.actor, clock=state.clock)
    request.state.records = 0
