from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from .config import settings
from .db import Base, SessionLocal, engine
from .routers import agent, authn, compliance, provision, repos, settings_ui, ui
from .seed import seed

app = FastAPI(title="Landscape", docs_url="/api/docs", openapi_url="/api/openapi.json")
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret,
                   max_age=12 * 3600, same_site="lax")


# Columns added after the initial release; create_all only makes new tables,
# so bring existing installs up to date with idempotent ALTERs.
MIGRATIONS = [
    "ALTER TABLE images ADD COLUMN IF NOT EXISTS iso_path VARCHAR(255) DEFAULT ''",
    "ALTER TABLE images ADD COLUMN IF NOT EXISTS config JSON DEFAULT '{}'",
    "ALTER TABLE machines ADD COLUMN IF NOT EXISTS overrides JSON DEFAULT '{}'",
]


@app.on_event("startup")
def startup():
    Base.metadata.create_all(engine)
    from sqlalchemy import text
    with engine.begin() as conn:
        for stmt in MIGRATIONS:
            conn.execute(text(stmt))
    db = SessionLocal()
    try:
        seed(db)
        # Render artifacts so a fresh install serves a valid boot menu and
        # dnsmasq config immediately.
        from .renderer import render_all, render_dnsmasq, render_mirror_list
        render_all(db)
        render_dnsmasq(db)
        render_mirror_list(db)
    finally:
        db.close()


@app.exception_handler(HTTPException)
async def auth_redirect(request: Request, exc: HTTPException):
    # Browser pages bounce to /login; API/agent callers get plain JSON.
    if exc.status_code == 401 and not request.url.path.startswith(("/agent", "/api")):
        return RedirectResponse("/login", status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


app.include_router(authn.router)
app.include_router(agent.router)
app.include_router(ui.router)
app.include_router(compliance.router)
app.include_router(repos.router)
app.include_router(provision.router)
app.include_router(settings_ui.router)


@app.get("/health")
def health():
    return {"ok": True}
