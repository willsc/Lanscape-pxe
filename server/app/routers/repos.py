"""Repository management: apt mirror definitions, sync control, and pushing
mirror-backed sources.list to managed hosts (offline updates)."""

import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import current_user, require_role
from ..db import get_db
from ..models import Host, RepoMirror, Task, User
from ..renderer import (client_sources_snippet, render_mirror_list,
                        repo_sync_status, trigger_repo_sync)
from ..web import templates

router = APIRouter(prefix="/repos", tags=["repos"])


@router.get("")
def repos_page(request: Request, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    mirrors = db.scalars(select(RepoMirror).order_by(RepoMirror.id)).all()
    enabled = [m for m in mirrors if m.enabled]
    suites = sorted({s for m in enabled for s in m.suites})
    components = sorted({c for m in enabled for c in m.components}) or ["main"]
    return templates.TemplateResponse(request, "repos.html", {
        "user": user, "active": "repos", "mirrors": mirrors,
        "status": repo_sync_status(),
        "snippet": client_sources_snippet(suites, components) if suites else "",
        "n_ubuntu_hosts": len(db.scalars(select(Host).where(Host.os_family == "ubuntu")).all()),
    })


@router.post("/new")
def mirror_new(name: str = Form(...), upstream: str = Form(...),
               suites: str = Form(...), components: str = Form("main"),
               arches: str = Form("amd64"),
               user: User = Depends(require_role("admin")),
               db: Session = Depends(get_db)):
    db.add(RepoMirror(name=name, upstream=upstream.rstrip("/"),
                      suites=suites.split(), components=components.split(),
                      arches=arches.split(), enabled=True))
    db.commit()
    render_mirror_list(db)
    return RedirectResponse("/repos", status_code=303)


@router.post("/{mirror_id}/toggle")
def mirror_toggle(mirror_id: int, user: User = Depends(require_role("admin")),
                  db: Session = Depends(get_db)):
    m = db.get(RepoMirror, mirror_id)
    if m:
        m.enabled = not m.enabled
        db.commit()
        render_mirror_list(db)
    return RedirectResponse("/repos", status_code=303)


@router.post("/{mirror_id}/delete")
def mirror_delete(mirror_id: int, user: User = Depends(require_role("admin")),
                  db: Session = Depends(get_db)):
    m = db.get(RepoMirror, mirror_id)
    if m:
        db.delete(m)
        db.commit()
        render_mirror_list(db)
    return RedirectResponse("/repos", status_code=303)


@router.post("/sync")
def sync_now(user: User = Depends(require_role("operator")),
             db: Session = Depends(get_db)):
    trigger_repo_sync(db)
    return RedirectResponse("/repos", status_code=303)


@router.post("/push-sources")
def push_sources(user: User = Depends(require_role("operator")),
                 db: Session = Depends(get_db)):
    """Point every managed Ubuntu host's apt at the local mirror."""
    mirrors = db.scalars(select(RepoMirror).where(RepoMirror.enabled.is_(True))).all()
    suites = sorted({s for m in mirrors for s in m.suites})
    components = sorted({c for m in mirrors for c in m.components}) or ["main"]
    if not suites:
        return RedirectResponse("/repos", status_code=303)
    snippet = client_sources_snippet(suites, components)
    for h in db.scalars(select(Host).where(Host.os_family == "ubuntu")).all():
        db.add(Task(host_id=h.id, type="repo_config",
                    payload={"sources": snippet}, created_by=user.username))
    db.commit()
    return RedirectResponse("/tasks", status_code=303)
