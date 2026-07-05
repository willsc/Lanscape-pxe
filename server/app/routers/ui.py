"""Core UI: login page, dashboard, hosts, tasks, CSV exports."""

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from ..auth import current_user, oidc_config, require_role
from ..config import settings
from ..db import get_db
from ..models import ComplianceProfile, ComplianceResult, Host, Image, Task, User
from ..web import online, templates

router = APIRouter(tags=["ui"])

HARDEN_ACTIONS = {
    "firewall": "Enable ufw (deny incoming, allow SSH)",
    "ssh": "Harden sshd (no root login, max 3 auth tries)",
    "sysctl": "Kernel/network sysctl hardening",
    "auto_updates": "Enable unattended security updates",
    "fail2ban": "Install and enable fail2ban",
    "auditd": "Install and enable auditd",
}


@router.get("/login")
def login_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(request, "login.html", {
        "sso": bool(oidc_config(db)),
        "error": request.query_params.get("error"),
    })


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    hosts = db.scalars(select(Host)).all()
    n_online = sum(1 for h in hosts if online(h))
    n_updates = sum(1 for h in hosts if h.updates)
    n_reboot = sum(1 for h in hosts if h.reboot_required)
    pending_tasks = db.scalar(select(func.count(Task.id)).where(Task.status.in_(["pending", "sent"])))
    recent_tasks = db.scalars(select(Task).order_by(desc(Task.id)).limit(8)).all()
    latest_compliance = db.scalars(select(ComplianceResult)
                                   .order_by(desc(ComplianceResult.id)).limit(8)).all()
    return templates.TemplateResponse(request, "dashboard.html", {
        "user": user, "active": "dashboard", "hosts": hosts, "n_online": n_online,
        "n_updates": n_updates, "n_reboot": n_reboot, "pending_tasks": pending_tasks,
        "recent_tasks": recent_tasks, "latest_compliance": latest_compliance,
    })


@router.get("/hosts")
def hosts_page(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    hosts = db.scalars(select(Host).order_by(Host.hostname)).all()
    profiles = db.scalars(select(ComplianceProfile)).all()
    return templates.TemplateResponse(request, "hosts.html", {
        "user": user, "active": "hosts", "hosts": hosts, "profiles": profiles,
        "harden_actions": HARDEN_ACTIONS,
    })


@router.get("/hosts/{host_id}")
def host_detail(host_id: int, request: Request, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    host = db.get(Host, host_id)
    if not host:
        return RedirectResponse("/hosts", status_code=303)
    tasks = db.scalars(select(Task).where(Task.host_id == host_id)
                       .order_by(desc(Task.id)).limit(30)).all()
    results = db.scalars(select(ComplianceResult).where(ComplianceResult.host_id == host_id)
                         .order_by(desc(ComplianceResult.id)).limit(10)).all()
    profiles = db.scalars(select(ComplianceProfile)
                          .where(ComplianceProfile.os_family == host.os_family)).all()
    q = (request.query_params.get("q") or "").lower()
    packages = [p for p in (host.packages or []) if q in p.get("name", "").lower()] if q \
        else (host.packages or [])
    return templates.TemplateResponse(request, "host_detail.html", {
        "user": user, "active": "hosts", "host": host, "tasks": tasks,
        "results": results, "profiles": profiles, "packages": packages[:400],
        "n_packages": len(host.packages or []), "q": q,
        "harden_actions": HARDEN_ACTIONS,
    })


def _new_task(db: Session, host: Host, type_: str, payload: dict, username: str) -> Task:
    t = Task(host_id=host.id, type=type_, payload=payload, created_by=username)
    db.add(t)
    db.commit()
    return t


@router.post("/hosts/{host_id}/action")
def host_action(host_id: int, request: Request,
                action: str = Form(...), arg: str = Form(""),
                harden: list[str] = Form([]),
                user: User = Depends(require_role("operator")),
                db: Session = Depends(get_db)):
    host = db.get(Host, host_id)
    if not host:
        return RedirectResponse("/hosts", status_code=303)
    if action == "pkg_install":
        _new_task(db, host, "pkg_install", {"packages": arg.split()}, user.username)
    elif action == "pkg_remove":
        _new_task(db, host, "pkg_remove", {"packages": arg.split()}, user.username)
    elif action == "upgrade":
        _new_task(db, host, "upgrade", {}, user.username)
    elif action == "script":
        _new_task(db, host, "script", {"script": arg}, user.username)
    elif action == "reboot":
        _new_task(db, host, "reboot", {}, user.username)
    elif action == "harden":
        actions = [a for a in (harden or list(HARDEN_ACTIONS)) if a in HARDEN_ACTIONS]
        _new_task(db, host, "harden", {"actions": actions}, user.username)
    elif action == "scan":
        profile = db.get(ComplianceProfile, int(arg))
        if profile:
            _new_task(db, host, "compliance_scan",
                      {"profile_id": profile.id, "rules": profile.rules}, user.username)
    elif action == "tags":
        host.tags = arg.strip()
        db.commit()
    elif action == "delete":
        db.delete(host)
        db.commit()
        return RedirectResponse("/hosts", status_code=303)
    return RedirectResponse(f"/hosts/{host_id}", status_code=303)


@router.post("/hosts/bulk")
async def hosts_bulk(request: Request,
                     user: User = Depends(require_role("operator")),
                     db: Session = Depends(get_db)):
    form = await request.form()
    action = form.get("action")
    ids = [int(i) for i in form.getlist("host_id")]
    arg = form.get("arg", "")
    for hid in ids:
        host = db.get(Host, hid)
        if not host:
            continue
        if action == "upgrade":
            _new_task(db, host, "upgrade", {}, user.username)
        elif action == "harden":
            _new_task(db, host, "harden", {"actions": list(HARDEN_ACTIONS)}, user.username)
        elif action == "scan" and arg:
            profile = db.get(ComplianceProfile, int(arg))
            if profile and profile.os_family == host.os_family:
                _new_task(db, host, "compliance_scan",
                          {"profile_id": profile.id, "rules": profile.rules}, user.username)
        elif action == "pkg_install" and arg:
            _new_task(db, host, "pkg_install", {"packages": arg.split()}, user.username)
    return RedirectResponse("/tasks", status_code=303)


@router.get("/software")
def software_page(request: Request, user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    hosts = db.scalars(select(Host).order_by(Host.hostname)).all()
    images = db.scalars(select(Image).order_by(Image.id)).all()
    pkg_tasks = db.scalars(select(Task).where(Task.type.in_(["pkg_install", "pkg_remove"]))
                           .order_by(desc(Task.id)).limit(30)).all()
    q = (request.query_params.get("q") or "").strip().lower()
    found = []
    if q:
        for h in hosts:
            for p in h.packages or []:
                if q in p.get("name", "").lower():
                    found.append((h, p.get("name", ""), p.get("version", "")))
    return templates.TemplateResponse(request, "software.html", {
        "user": user, "active": "software", "hosts": hosts, "images": images,
        "pkg_tasks": pkg_tasks, "q": q, "found": found[:200],
        "server_ip": settings.server_ip, "http_port": settings.http_port,
    })


@router.post("/software/action")
async def software_action(request: Request,
                          user: User = Depends(require_role("operator")),
                          db: Session = Depends(get_db)):
    form = await request.form()
    action = form.get("action")
    packages = (form.get("packages") or "").split()
    target = form.get("target", "selected")
    ids = [int(i) for i in form.getlist("host_id")]
    if action not in ("pkg_install", "pkg_remove") or not packages:
        return RedirectResponse("/software", status_code=303)
    for host in db.scalars(select(Host)).all():
        if target == "all" or target == host.os_family \
                or (target == "selected" and host.id in ids):
            _new_task(db, host, action, {"packages": packages}, user.username)
    return RedirectResponse("/software", status_code=303)


@router.get("/tasks")
def tasks_page(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    tasks = db.scalars(select(Task).order_by(desc(Task.id)).limit(200)).all()
    return templates.TemplateResponse(request, "tasks.html", {
        "user": user, "active": "tasks", "tasks": tasks,
    })


@router.get("/tasks/{task_id}")
def task_detail(task_id: int, request: Request, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    task = db.get(Task, task_id)
    if not task:
        return RedirectResponse("/tasks", status_code=303)
    return templates.TemplateResponse(request, "task_detail.html", {
        "user": user, "active": "tasks", "task": task,
    })


# --- CSV exports (reporting) ---

def _csv_response(rows: list[list], filename: str) -> StreamingResponse:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    buf.seek(0)
    return StreamingResponse(buf, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/export/hosts.csv")
def export_hosts(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = [["hostname", "os", "version", "kernel", "arch", "ip", "mac", "cpu",
             "memory_mb", "disks", "pending_updates", "reboot_required", "last_seen", "tags"]]
    for h in db.scalars(select(Host).order_by(Host.hostname)).all():
        hw = h.hardware or {}
        rows.append([h.hostname, h.os_family, h.os_version, h.kernel, h.arch, h.ip,
                     h.mac, hw.get("cpu", ""), hw.get("memory_mb", ""),
                     "; ".join(f"{d.get('name')}:{d.get('size')}" for d in hw.get("disks", [])),
                     len(h.updates or []), h.reboot_required,
                     h.last_seen.isoformat() if h.last_seen else "", h.tags])
    return _csv_response(rows, "hosts.csv")


@router.get("/export/packages.csv")
def export_packages(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = [["hostname", "package", "version"]]
    for h in db.scalars(select(Host)).all():
        for p in h.packages or []:
            rows.append([h.hostname, p.get("name"), p.get("version")])
    return _csv_response(rows, "packages.csv")


@router.get("/export/compliance.csv")
def export_compliance(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = [["hostname", "profile", "ran_at", "rule", "description", "ok", "detail"]]
    seen: set[tuple[int, int]] = set()
    for r in db.scalars(select(ComplianceResult).order_by(desc(ComplianceResult.id))).all():
        key = (r.host_id, r.profile_id)
        if key in seen:  # latest result per host+profile only
            continue
        seen.add(key)
        for item in r.results or []:
            rows.append([r.host.hostname, r.profile.name, r.ran_at.isoformat(),
                         item.get("id"), item.get("desc"), item.get("ok"),
                         item.get("detail", "")])
    return _csv_response(rows, "compliance.csv")
