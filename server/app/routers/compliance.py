"""Compliance profiles (rule sets) and fleet reports."""

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import current_user, require_role
from ..db import get_db
from ..models import ComplianceProfile, ComplianceResult, Host, Task, User
from ..web import templates

router = APIRouter(prefix="/compliance", tags=["compliance"])


@router.get("")
def compliance_page(request: Request, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    profiles = db.scalars(select(ComplianceProfile).order_by(ComplianceProfile.id)).all()
    hosts = {h.id: h for h in db.scalars(select(Host)).all()}

    # Latest result per (host, profile).
    latest: dict[tuple[int, int], ComplianceResult] = {}
    for r in db.scalars(select(ComplianceResult).order_by(desc(ComplianceResult.id))).all():
        latest.setdefault((r.host_id, r.profile_id), r)

    summary = []
    for p in profiles:
        rs = [r for (hid, pid), r in latest.items() if pid == p.id and hid in hosts]
        n_pass = sum(1 for r in rs if r.failed == 0)
        summary.append({"profile": p, "n_hosts": len(rs), "n_pass": n_pass,
                        "results": sorted(rs, key=lambda r: hosts[r.host_id].hostname)})
    return templates.TemplateResponse(request, "compliance.html", {
        "user": user, "active": "compliance", "profiles": profiles,
        "summary": summary, "hosts": hosts,
    })


@router.get("/profiles/{profile_id}")
def profile_page(profile_id: int, request: Request,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(ComplianceProfile, profile_id)
    if not p:
        return RedirectResponse("/compliance", status_code=303)
    return templates.TemplateResponse(request, "compliance_profile.html", {
        "user": user, "active": "compliance", "p": p,
        "rules_json": json.dumps(p.rules, indent=2),
    })


@router.post("/profiles/new")
def profile_new(name: str = Form(...), os_family: str = Form("ubuntu"),
                description: str = Form(""),
                user: User = Depends(require_role("admin")),
                db: Session = Depends(get_db)):
    p = ComplianceProfile(name=name, os_family=os_family, description=description, rules=[])
    db.add(p)
    db.commit()
    return RedirectResponse(f"/compliance/profiles/{p.id}", status_code=303)


@router.post("/profiles/{profile_id}")
def profile_save(profile_id: int, description: str = Form(""),
                 rules_json: str = Form("[]"),
                 user: User = Depends(require_role("admin")),
                 db: Session = Depends(get_db)):
    p = db.get(ComplianceProfile, profile_id)
    if p:
        try:
            rules = json.loads(rules_json)
            assert isinstance(rules, list)
        except Exception:
            return RedirectResponse(f"/compliance/profiles/{profile_id}?error=json",
                                    status_code=303)
        p.description = description
        p.rules = rules
        db.commit()
    return RedirectResponse(f"/compliance/profiles/{profile_id}", status_code=303)


@router.post("/profiles/{profile_id}/delete")
def profile_delete(profile_id: int, user: User = Depends(require_role("admin")),
                   db: Session = Depends(get_db)):
    p = db.get(ComplianceProfile, profile_id)
    if p and not p.builtin:
        db.delete(p)
        db.commit()
    return RedirectResponse("/compliance", status_code=303)


@router.post("/scan/{profile_id}")
def scan_all(profile_id: int, user: User = Depends(require_role("operator")),
             db: Session = Depends(get_db)):
    """Queue a scan of this profile on every matching host."""
    p = db.get(ComplianceProfile, profile_id)
    if p:
        for h in db.scalars(select(Host).where(Host.os_family == p.os_family)).all():
            db.add(Task(host_id=h.id, type="compliance_scan",
                        payload={"profile_id": p.id, "rules": p.rules},
                        created_by=user.username))
        db.commit()
    return RedirectResponse("/compliance", status_code=303)
