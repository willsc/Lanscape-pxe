"""Sessions, local password auth, BYO-OIDC, role enforcement.

SSO is deliberately "bring your own": any OIDC provider (Keycloak, Authentik,
Entra ID, Okta, ...) configured at runtime from the Settings page. Roles map
from a groups claim; local accounts remain as break-glass fallback.
"""

import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import Setting, User

ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}


# --- password hashing (stdlib pbkdf2 — no compiled deps, offline-friendly) ---

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return f"pbkdf2${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return hmac.compare_digest(dk.hex(), digest)


# --- settings helpers ---

def get_setting(db: Session, key: str, default: dict | None = None) -> dict:
    row = db.get(Setting, key)
    return dict(row.value) if row else (default or {})


def put_setting(db: Session, key: str, value: dict) -> None:
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def oidc_config(db: Session) -> dict:
    """Returns {} when SSO is not configured."""
    cfg = get_setting(db, "oidc")
    if cfg.get("issuer") and cfg.get("client_id"):
        return cfg
    return {}


def map_oidc_role(cfg: dict, claims: dict) -> str:
    groups = claims.get(cfg.get("groups_claim") or "groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if cfg.get("admin_group") and cfg["admin_group"] in groups:
        return "admin"
    if cfg.get("operator_group") and cfg["operator_group"] in groups:
        return "operator"
    return cfg.get("default_role") or "viewer"


# --- request auth ---

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="not authenticated")
    user = db.get(User, uid)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


def require_role(min_role: str):
    def dep(user: User = Depends(current_user)) -> User:
        if ROLE_RANK.get(user.role, 0) < ROLE_RANK[min_role]:
            raise HTTPException(status_code=403, detail=f"requires {min_role} role")
        return user

    return dep
