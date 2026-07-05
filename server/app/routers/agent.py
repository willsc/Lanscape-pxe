"""Agent-facing API. Agents authenticate with (host_id, agent_key) issued at
enrollment; enrollment itself needs an active enroll token."""

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import ComplianceResult, EnrollToken, Host, Machine, Task

router = APIRouter(prefix="/agent", tags=["agent"])


def utcnow():
    return datetime.now(timezone.utc)


class EnrollIn(BaseModel):
    token: str
    machine_id: str
    hostname: str
    os_family: str = "ubuntu"
    os_version: str = ""
    kernel: str = ""
    arch: str = ""
    ip: str = ""
    mac: str = ""
    agent_version: str = ""


@router.post("/enroll")
def enroll(body: EnrollIn, db: Session = Depends(get_db)):
    tok = db.scalar(select(EnrollToken).where(EnrollToken.token == body.token,
                                              EnrollToken.active.is_(True)))
    if not tok:
        raise HTTPException(403, "invalid enroll token")

    host = db.scalar(select(Host).where(Host.machine_id == body.machine_id))
    if not host:
        host = Host(machine_id=body.machine_id)
        db.add(host)
    for f in ("hostname", "os_family", "os_version", "kernel", "arch", "ip", "mac",
              "agent_version"):
        setattr(host, f, getattr(body, f))
    host.last_seen = utcnow()
    db.commit()

    # Link back to the provisioning record if this box was PXE-installed.
    if host.mac:
        m = db.scalar(select(Machine).where(Machine.mac == host.mac.lower()))
        if m:
            m.status = "installed"
            db.commit()

    return {"host_id": host.id, "agent_key": host.agent_key}


def auth_host(db: Session, host_id: int, agent_key: str) -> Host:
    host = db.get(Host, host_id)
    if not host or host.agent_key != agent_key:
        raise HTTPException(403, "bad host credentials")
    return host


class CheckinIn(BaseModel):
    hostname: str | None = None
    os_version: str | None = None
    kernel: str | None = None
    arch: str | None = None
    ip: str | None = None
    mac: str | None = None
    agent_version: str | None = None
    hardware: dict | None = None
    packages: list | None = None
    updates: list | None = None
    reboot_required: bool | None = None


@router.post("/checkin")
def checkin(body: CheckinIn,
            x_host_id: int = Header(...), x_agent_key: str = Header(...),
            db: Session = Depends(get_db)):
    host = auth_host(db, x_host_id, x_agent_key)
    for f in ("hostname", "os_version", "kernel", "arch", "ip", "mac",
              "agent_version", "hardware", "packages", "updates", "reboot_required"):
        v = getattr(body, f)
        if v is not None:
            setattr(host, f, v)
    host.last_seen = utcnow()

    tasks = db.scalars(select(Task).where(Task.host_id == host.id,
                                          Task.status == "pending")
                       .order_by(Task.id)).all()
    out = [{"id": t.id, "type": t.type, "payload": t.payload} for t in tasks]
    for t in tasks:
        t.status = "sent"
    db.commit()
    return {"tasks": out}


class ResultIn(BaseModel):
    task_id: int
    status: str  # done | failed
    exit_code: int | None = None
    output: str = ""


@router.post("/result")
def result(body: ResultIn,
           x_host_id: int = Header(...), x_agent_key: str = Header(...),
           db: Session = Depends(get_db)):
    host = auth_host(db, x_host_id, x_agent_key)
    task = db.get(Task, body.task_id)
    if not task or task.host_id != host.id:
        raise HTTPException(404, "no such task")
    task.status = "done" if body.status == "done" else "failed"
    task.exit_code = body.exit_code
    task.output = body.output[-200_000:]
    task.finished_at = utcnow()

    # A compliance scan's output is the JSON rule-result list — persist it as
    # a ComplianceResult so the reports pages can aggregate it.
    if task.type == "compliance_scan":
        try:
            results = json.loads(body.output)
            passed = sum(1 for r in results if r.get("ok"))
            db.add(ComplianceResult(host_id=host.id,
                                    profile_id=task.payload.get("profile_id"),
                                    passed=passed, failed=len(results) - passed,
                                    results=results))
        except Exception:
            task.status = "failed"
    db.commit()
    return {"ok": True}
