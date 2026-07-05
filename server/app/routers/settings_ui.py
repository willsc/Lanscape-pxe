"""Settings: BYO SSO/IAM (OIDC), enrollment tokens, local users."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import get_setting, hash_password, put_setting, require_role
from ..config import settings as cfg
from ..db import get_db
from ..models import EnrollToken, User
from ..renderer import provisioning_token
from ..web import templates

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def settings_page(request: Request, user: User = Depends(require_role("admin")),
                  db: Session = Depends(get_db)):
    provisioning_token(db)  # make sure the auto token exists
    return templates.TemplateResponse(request, "settings.html", {
        "user": user, "active": "settings",
        "oidc": get_setting(db, "oidc"),
        "tokens": db.scalars(select(EnrollToken).order_by(EnrollToken.id)).all(),
        "users": db.scalars(select(User).order_by(User.id)).all(),
        "server_ip": cfg.server_ip, "web_port": cfg.web_port,
        "http_port": cfg.http_port,
    })


@router.post("/oidc")
def save_oidc(issuer: str = Form(""), client_id: str = Form(""),
              client_secret: str = Form(""), scopes: str = Form("openid profile email"),
              groups_claim: str = Form("groups"), admin_group: str = Form(""),
              operator_group: str = Form(""), default_role: str = Form("viewer"),
              user: User = Depends(require_role("admin")),
              db: Session = Depends(get_db)):
    put_setting(db, "oidc", {
        "issuer": issuer.strip(), "client_id": client_id.strip(),
        "client_secret": client_secret.strip(), "scopes": scopes.strip(),
        "groups_claim": groups_claim.strip() or "groups",
        "admin_group": admin_group.strip(), "operator_group": operator_group.strip(),
        "default_role": default_role if default_role in ("viewer", "operator", "admin") else "viewer",
    })
    return RedirectResponse("/settings", status_code=303)


@router.post("/tokens/new")
def token_new(note: str = Form(""), user: User = Depends(require_role("admin")),
              db: Session = Depends(get_db)):
    db.add(EnrollToken(note=note))
    db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/tokens/{token_id}/toggle")
def token_toggle(token_id: int, user: User = Depends(require_role("admin")),
                 db: Session = Depends(get_db)):
    t = db.get(EnrollToken, token_id)
    if t:
        t.active = not t.active
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/users/new")
def user_new(username: str = Form(...), password: str = Form(...),
             role: str = Form("viewer"),
             user: User = Depends(require_role("admin")),
             db: Session = Depends(get_db)):
    if not db.scalar(select(User).where(User.username == username)):
        db.add(User(username=username.strip(), pw_hash=hash_password(password),
                    role=role if role in ("viewer", "operator", "admin") else "viewer",
                    source="local"))
        db.commit()
    return RedirectResponse("/settings", status_code=303)


@router.post("/users/{user_id}/delete")
def user_delete(user_id: int, user: User = Depends(require_role("admin")),
                db: Session = Depends(get_db)):
    u = db.get(User, user_id)
    if u and u.id != user.id:
        db.delete(u)
        db.commit()
    return RedirectResponse("/settings", status_code=303)
