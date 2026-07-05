"""Login/logout: local accounts + bring-your-own OIDC (configured in the UI)."""

from authlib.integrations.starlette_client import OAuth
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import map_oidc_role, oidc_config, verify_password
from ..db import get_db
from ..models import User

router = APIRouter(tags=["auth"])


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...),
          db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == username,
                                        User.source == "local"))
    if not user or not user.pw_hash or not verify_password(password, user.pw_hash):
        return RedirectResponse("/login?error=1", status_code=303)
    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _oauth_client(cfg: dict):
    oauth = OAuth()
    issuer = cfg["issuer"].rstrip("/")
    return oauth.register(
        name="sso",
        client_id=cfg["client_id"],
        client_secret=cfg.get("client_secret", ""),
        server_metadata_url=f"{issuer}/.well-known/openid-configuration",
        client_kwargs={"scope": cfg.get("scopes") or "openid profile email"},
    )


@router.get("/oidc/login")
async def oidc_login(request: Request, db: Session = Depends(get_db)):
    cfg = oidc_config(db)
    if not cfg:
        return RedirectResponse("/login?error=sso", status_code=303)
    client = _oauth_client(cfg)
    redirect_uri = str(request.url_for("oidc_callback"))
    return await client.authorize_redirect(request, redirect_uri)


@router.get("/oidc/callback")
async def oidc_callback(request: Request, db: Session = Depends(get_db)):
    cfg = oidc_config(db)
    if not cfg:
        return RedirectResponse("/login?error=sso", status_code=303)
    client = _oauth_client(cfg)
    try:
        token = await client.authorize_access_token(request)
        claims = token.get("userinfo") or {}
        if not claims:
            claims = await client.userinfo(token=token)
    except Exception:
        return RedirectResponse("/login?error=sso", status_code=303)

    username = claims.get("preferred_username") or claims.get("email") or claims.get("sub")
    if not username:
        return RedirectResponse("/login?error=sso", status_code=303)

    user = db.scalar(select(User).where(User.username == username,
                                        User.source == "oidc"))
    role = map_oidc_role(cfg, claims)
    if not user:
        user = User(username=username, email=claims.get("email"),
                    role=role, source="oidc")
        db.add(user)
    else:
        user.role = role  # groups claim is authoritative on every login
        user.email = claims.get("email") or user.email
    db.commit()

    request.session["uid"] = user.id
    return RedirectResponse("/", status_code=303)
