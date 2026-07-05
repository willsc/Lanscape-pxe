"""Renders on-disk artifacts the sidecar containers serve:

- boot.ipxe + per-machine iPXE / cloud-init / autounattend  -> ./http
- dnsmasq.conf (proxy / server / auto-resolved)             -> ./config
- mirror.list for apt-mirror + sync trigger                 -> ./repo
"""

import base64
import json
import pathlib
from datetime import datetime, timezone

import httpx
from jinja2 import Environment, PackageLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import get_setting
from .config import settings
from .models import EnrollToken, Machine, RepoMirror

env = Environment(
    loader=PackageLoader("app", "templates/pxe"),
    autoescape=select_autoescape(["xml"]),
    keep_trailing_newline=True,
)

WINDOWS_TZ = "GMT Standard Time"
TIMEZONE = "Europe/London"

# Base install-time config; image.config overrides these, machine.overrides
# override both.
DEFAULT_CFG = {
    "ubuntu": {
        "locale": "en_US.UTF-8", "keyboard": "us", "timezone": TIMEZONE,
        "storage": "direct", "kernel_args": "", "post_script": "",
        "use_local_mirror": False, "install_agent": True,
        "username": "ubuntu", "password_hash": "",
    },
    "windows": {
        "locale": "en-US", "timezone_windows": WINDOWS_TZ,
        "product_key": "W269N-WFGWX-YVC9B-4J6C9-T83GX",  # Pro KMS placeholder
        "image_name": "Windows 11 Pro", "post_script": "",
        "install_agent": True, "username": "Admin", "password": "",
    },
}


# First-logon package installer: waits for winget to become available (App
# Installer registers shortly after OOBE), then installs each ID silently.
WINGET_SCRIPT = """$log = 'C:\\Windows\\Temp\\landscape-packages.log'
"start $(Get-Date)" | Add-Content $log
foreach ($i in 1..40) {{
  if (Get-Command winget -ErrorAction SilentlyContinue) {{ break }}
  Start-Sleep -Seconds 15
}}
foreach ($p in @({pkg_list})) {{
  "installing $p" | Add-Content $log
  winget install -e --id $p --silent --accept-package-agreements --accept-source-agreements --source winget 2>&1 | Out-String | Add-Content $log
}}
"done $(Get-Date)" | Add-Content $log
"""


def winget_script_b64(packages: list[str]) -> str:
    if not packages:
        return ""
    pkg_list = ", ".join("'" + p.replace("'", "''") + "'" for p in packages)
    script = WINGET_SCRIPT.format(pkg_list=pkg_list)
    return base64.b64encode(script.encode("utf-16-le")).decode()


def effective_config(image, machine=None) -> dict:
    cfg = dict(DEFAULT_CFG.get(image.os_family if image else "ubuntu", {}))
    if image and image.config:
        cfg.update({k: v for k, v in image.config.items() if v not in (None, "")})
    if machine is not None:
        if machine.overrides:
            cfg.update({k: v for k, v in machine.overrides.items() if v not in (None, "")})
        if not machine.enroll_on_install:
            cfg["install_agent"] = False
    return cfg


def provisioning_token(db: Session) -> str:
    """A long-lived enroll token freshly-installed machines use to join."""
    tok = db.scalar(select(EnrollToken).where(EnrollToken.note == "provisioning (auto)",
                                              EnrollToken.active.is_(True)))
    if not tok:
        tok = EnrollToken(note="provisioning (auto)")
        db.add(tok)
        db.commit()
    return tok.token


def _ctx(db: Session) -> dict:
    return {
        "server_ip": settings.server_ip,
        "http_port": settings.http_port,
        "web_port": settings.web_port,
        "enroll_token": provisioning_token(db),
        "timezone": TIMEZONE,
        "windows_timezone": WINDOWS_TZ,
    }


def render_boot_menu(db: Session) -> str:
    machines = db.scalars(select(Machine).order_by(Machine.hostname)).all()
    text = env.get_template("boot.ipxe.j2").render(machines=machines, **_ctx(db))
    root = pathlib.Path(settings.http_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / "boot.ipxe").write_text(text)
    return text


def render_machine(db: Session, machine: Machine) -> None:
    image = machine.image
    ctx = _ctx(db)
    root = pathlib.Path(settings.http_dir)
    clients = root / "clients"
    clients.mkdir(parents=True, exist_ok=True)

    cfg = effective_config(image, machine) if image else {}
    ipxe = env.get_template("machine.ipxe.j2").render(machine=machine, image=image,
                                                      cfg=cfg, **ctx)
    (clients / f"{machine.mac_hyph}.ipxe").write_text(ipxe)

    per = clients / machine.mac_hyph
    per.mkdir(exist_ok=True)
    if image and image.os_family == "ubuntu":
        packages = machine.packages or image.default_packages or []
        snaps = (machine.overrides or {}).get("snaps") or cfg.get("snaps") or []
        ud = env.get_template("user_data.j2").render(
            machine=machine, image=image, packages=packages, snaps=snaps, cfg=cfg,
            username=machine.username or cfg.get("username", "ubuntu"),
            password_hash=machine.password or cfg.get("password_hash", ""),
            net=(machine.overrides or {}).get("network") or None,
            post_script_b64=base64.b64encode(
                cfg.get("post_script", "").encode()).decode(),
            **ctx)
        (per / "user-data").write_text(ud)
        (per / "meta-data").write_text(f"instance-id: {machine.mac_hyph}\n")
    elif image and image.os_family == "windows":
        post_b64 = ""
        if cfg.get("post_script"):
            post_b64 = base64.b64encode(
                cfg["post_script"].encode("utf-16-le")).decode()
        packages = machine.packages or image.default_packages or []
        xml = env.get_template("autounattend.xml.j2").render(
            machine=machine, image=image, cfg=cfg,
            username=machine.username or cfg.get("username", "Admin"),
            password=machine.password or cfg.get("password", ""),
            post_script_b64=post_b64,
            winget_b64=winget_script_b64(packages), **ctx)
        (per / "autounattend.xml").write_text(xml)
    machine.status = "rendered"
    db.commit()


def render_all(db: Session) -> None:
    render_boot_menu(db)
    for m in db.scalars(select(Machine)).all():
        render_machine(db, m)


# --- DHCP / dnsmasq ---

def probe_dhcp(timeout: int = 4) -> dict:
    try:
        r = httpx.get(f"{settings.probe_url}/probe", params={"timeout": timeout},
                      timeout=timeout + 6)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"found": False, "offers": [{"error": repr(e)}], "interface": None}


def render_dnsmasq(db: Session) -> tuple[str, str]:
    """Render dnsmasq.conf; returns (text, effective_mode)."""
    dhcp = get_setting(db, "dhcp")
    mode = dhcp.get("mode", "proxy")
    resolved = mode
    if mode == "auto":
        # An existing DHCP server on the LAN? Then proxy; otherwise serve.
        resolved = "proxy" if probe_dhcp().get("found") else "server"
    effective = resolved
    if effective == "server" and not (dhcp.get("range_start") and dhcp.get("range_end")):
        # No lease range configured — refuse to become the DHCP server.
        effective = "proxy"
    text = env.get_template("dnsmasq.conf.j2").render(
        dhcp=dhcp, effective_mode=effective, resolved_mode=effective,
        subnet=settings.subnet, server_ip=settings.server_ip,
        http_port=settings.http_port,
    )
    path = pathlib.Path(settings.dnsmasq_conf)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return text, effective


def restart_dnsmasq() -> bool:
    """Bounce the dnsmasq container over the mounted docker socket."""
    try:
        transport = httpx.HTTPTransport(uds=settings.docker_sock)
        with httpx.Client(transport=transport, base_url="http://docker") as c:
            r = c.post(f"/containers/{settings.dnsmasq_container}/restart", timeout=30)
            return r.status_code in (204, 304)
    except Exception:
        return False


# --- apt mirror ---

def render_mirror_list(db: Session) -> str:
    mirrors = db.scalars(select(RepoMirror).where(RepoMirror.enabled.is_(True))).all()
    lines = [
        "############# config ##################",
        "set base_path    /srv/repo/apt-mirror",
        "set mirror_path  $base_path/mirror",
        "set skel_path    $base_path/skel",
        "set var_path     $base_path/var",
        "set defaultarch  amd64",
        "set nthreads     10",
        "set _tilde 0",
        "############# end config ##############",
        "",
    ]
    for m in mirrors:
        arches = m.arches or ["amd64"]
        for suite in m.suites:
            for arch in arches:
                lines.append(f"deb-{arch} {m.upstream} {suite} {' '.join(m.components)}")
    lines.append("")
    lines.append("clean http://archive.ubuntu.com/ubuntu")
    text = "\n".join(lines) + "\n"
    repo = pathlib.Path(settings.repo_dir)
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "mirror.list").write_text(text)
    return text


def trigger_repo_sync(db: Session) -> None:
    render_mirror_list(db)
    repo = pathlib.Path(settings.repo_dir)
    (repo / "sync.trigger").write_text(datetime.now(timezone.utc).isoformat())


def repo_sync_status() -> dict:
    p = pathlib.Path(settings.repo_dir) / "status.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"state": "never-run"}


def client_sources_snippet(suites: list[str], components: list[str]) -> str:
    base = f"http://{settings.server_ip}:{settings.http_port}/repo/archive.ubuntu.com/ubuntu"
    return "\n".join(
        f"deb [trusted=yes] {base} {suite} {' '.join(components)}" for suite in suites
    ) + "\n"
