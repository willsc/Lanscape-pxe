"""Provisioning: OS images (with in-UI ISO builds + install customisation),
target machines (by MAC, with per-machine overrides), artifact rendering,
and the PXE / DHCP mode control."""

import crypt
import pathlib

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ..auth import current_user, get_setting, put_setting, require_role
from ..builder import build_running, list_isos, start_build
from ..config import settings
from ..db import get_db
from ..models import Image, Job, Machine, User
from ..renderer import (effective_config, probe_dhcp, render_all,
                        render_boot_menu, render_dnsmasq, render_machine,
                        restart_dnsmasq)
from ..web import templates

router = APIRouter(tags=["provision"])


def _image_ready(img: Image) -> bool:
    d = pathlib.Path(settings.http_dir) / img.slug
    if img.os_family == "ubuntu":
        return (d / "vmlinuz").exists() and (d / "initrd").exists()
    return (d / "sources" / "boot.wim").exists()


@router.get("/provision")
def provision_page(request: Request, user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    images = db.scalars(select(Image).order_by(Image.id)).all()
    machines = db.scalars(select(Machine).order_by(Machine.hostname)).all()
    jobs = db.scalars(select(Job).order_by(desc(Job.id)).limit(10)).all()
    return templates.TemplateResponse(request, "provision.html", {
        "user": user, "active": "provision", "images": images,
        "machines": machines, "image_ready": {i.id: _image_ready(i) for i in images},
        "building": {i.id: build_running(db, i.id) for i in images},
        "isos": list_isos(), "jobs": jobs,
        "server_ip": settings.server_ip, "http_port": settings.http_port,
    })


# --- images -------------------------------------------------------------

@router.post("/provision/images/new")
def image_new(name: str = Form(...), os_family: str = Form(...),
              version: str = Form(""), slug: str = Form(...),
              default_packages: str = Form(""),
              user: User = Depends(require_role("admin")),
              db: Session = Depends(get_db)):
    img = Image(name=name, os_family=os_family, version=version,
                slug=slug.strip().lower(), default_packages=default_packages.split())
    db.add(img)
    db.commit()
    return RedirectResponse(f"/provision/images/{img.id}", status_code=303)


@router.get("/provision/images/{image_id}")
def image_page(image_id: int, request: Request,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if not img:
        return RedirectResponse("/provision", status_code=303)
    jobs = db.scalars(select(Job).where(Job.ref_id == image_id,
                                        Job.kind == "image_build")
                      .order_by(desc(Job.id)).limit(5)).all()
    return templates.TemplateResponse(request, "image_detail.html", {
        "user": user, "active": "provision", "img": img,
        "cfg": effective_config(img), "ready": _image_ready(img),
        "building": build_running(db, image_id), "isos": list_isos(),
        "jobs": jobs, "server_ip": settings.server_ip,
    })


@router.post("/provision/images/{image_id}")
def image_save(image_id: int, request: Request,
               name: str = Form(...), version: str = Form(""),
               default_packages: str = Form(""), notes: str = Form(""),
               locale: str = Form(""), keyboard: str = Form(""),
               timezone: str = Form(""), timezone_windows: str = Form(""),
               storage: str = Form(""), kernel_args: str = Form(""),
               post_script: str = Form(""), username: str = Form(""),
               password: str = Form(""), product_key: str = Form(""),
               image_name: str = Form(""), snaps: str = Form(""),
               use_local_mirror: str = Form(""), install_agent: str = Form(""),
               user: User = Depends(require_role("admin")),
               db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if not img:
        return RedirectResponse("/provision", status_code=303)
    img.name, img.version, img.notes = name, version, notes
    img.default_packages = default_packages.split()
    cfg = dict(img.config or {})
    cfg.update({
        "locale": locale.strip(), "kernel_args": kernel_args.strip(),
        "post_script": post_script.replace("\r\n", "\n").strip(),
        "username": username.strip(),
        "use_local_mirror": bool(use_local_mirror),
        "install_agent": bool(install_agent),
    })
    if img.os_family == "ubuntu":
        cfg["keyboard"] = keyboard.strip()
        cfg["timezone"] = timezone.strip()
        cfg["storage"] = storage if storage in ("direct", "lvm") else "direct"
        cfg["snaps"] = snaps.split()
        if password:
            cfg["password_hash"] = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    else:
        cfg["timezone_windows"] = timezone_windows.strip()
        cfg["product_key"] = product_key.strip()
        cfg["image_name"] = image_name.strip()
        if password:
            cfg["password"] = password
    img.config = cfg
    db.commit()
    render_all(db)  # existing machines pick up new image defaults
    return RedirectResponse(f"/provision/images/{image_id}", status_code=303)


@router.post("/provision/images/{image_id}/build")
def image_build(image_id: int, iso_name: str = Form(...),
                user: User = Depends(require_role("admin")),
                db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if img and not build_running(db, image_id):
        start_build(db, img, iso_name)
    return RedirectResponse(f"/provision/images/{image_id}", status_code=303)


@router.post("/provision/images/{image_id}/delete")
def image_delete(image_id: int, user: User = Depends(require_role("admin")),
                 db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if img:
        db.delete(img)
        db.commit()
    return RedirectResponse("/provision", status_code=303)


@router.get("/provision/jobs/{job_id}")
def job_status(job_id: int, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        return JSONResponse({"error": "no such job"}, status_code=404)
    return {"id": job.id, "status": job.status, "title": job.title,
            "log": job.log or ""}


# --- machines -----------------------------------------------------------

@router.post("/provision/machines/new")
def machine_new(mac: str = Form(...), hostname: str = Form(...),
                image_id: int = Form(...), username: str = Form(""),
                password: str = Form(""), packages: str = Form(""),
                user: User = Depends(require_role("operator")),
                db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    stored = password
    if password and img and img.os_family == "ubuntu":
        stored = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512))
    m = Machine(mac=mac.strip().lower().replace("-", ":"), hostname=hostname.strip(),
                image_id=image_id, username=username.strip(),
                password=stored, packages=packages.split())
    db.add(m)
    db.commit()
    render_machine(db, m)
    render_boot_menu(db)
    return RedirectResponse(f"/provision/machines/{m.id}", status_code=303)


@router.get("/provision/machines/{machine_id}")
def machine_page(machine_id: int, request: Request,
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    m = db.get(Machine, machine_id)
    if not m:
        return RedirectResponse("/provision", status_code=303)
    images = db.scalars(select(Image).order_by(Image.id)).all()
    per = pathlib.Path(settings.http_dir) / "clients" / m.mac_hyph
    artifacts = {}
    for f in ("user-data", "autounattend.xml"):
        p = per / f
        if p.exists():
            artifacts[f] = p.read_text()
    return templates.TemplateResponse(request, "machine_detail.html", {
        "user": user, "active": "provision", "m": m, "images": images,
        "cfg": effective_config(m.image, m) if m.image else {},
        "ov": m.overrides or {}, "net": (m.overrides or {}).get("network") or {},
        "artifacts": artifacts,
    })


@router.post("/provision/machines/{machine_id}")
def machine_save(machine_id: int,
                 hostname: str = Form(...), image_id: int = Form(...),
                 username: str = Form(""), password: str = Form(""),
                 packages: str = Form(""), enroll: str = Form(""),
                 locale: str = Form(""), timezone: str = Form(""),
                 storage: str = Form(""), kernel_args: str = Form(""),
                 post_script: str = Form(""), snaps: str = Form(""),
                 net_ip: str = Form(""), net_prefix: str = Form(""),
                 net_gateway: str = Form(""), net_dns: str = Form(""),
                 user: User = Depends(require_role("operator")),
                 db: Session = Depends(get_db)):
    m = db.get(Machine, machine_id)
    if not m:
        return RedirectResponse("/provision", status_code=303)
    img = db.get(Image, image_id)
    m.hostname, m.image_id = hostname.strip(), image_id
    m.username = username.strip()
    m.packages = packages.split()
    m.enroll_on_install = bool(enroll)
    if password:
        m.password = crypt.crypt(password, crypt.mksalt(crypt.METHOD_SHA512)) \
            if img and img.os_family == "ubuntu" else password
    ov = {k: v for k, v in {
        "locale": locale.strip(), "timezone": timezone.strip(),
        "storage": storage if storage in ("", "direct", "lvm") else "",
        "kernel_args": kernel_args.strip(),
        "post_script": post_script.replace("\r\n", "\n").strip(),
    }.items() if v}
    if snaps.strip():
        ov["snaps"] = snaps.split()
    if net_ip.strip():
        ov["network"] = {"ip": net_ip.strip(),
                         "prefix": int(net_prefix or 24),
                         "gateway": net_gateway.strip(),
                         "dns": net_dns.split()}
    m.overrides = ov
    db.commit()
    render_machine(db, m)
    render_boot_menu(db)
    return RedirectResponse(f"/provision/machines/{machine_id}", status_code=303)


@router.post("/provision/machines/{machine_id}/delete")
def machine_delete(machine_id: int, user: User = Depends(require_role("operator")),
                   db: Session = Depends(get_db)):
    m = db.get(Machine, machine_id)
    if m:
        clients = pathlib.Path(settings.http_dir) / "clients"
        (clients / f"{m.mac_hyph}.ipxe").unlink(missing_ok=True)
        db.delete(m)
        db.commit()
        render_boot_menu(db)
    return RedirectResponse("/provision", status_code=303)


@router.post("/provision/render")
def rerender(user: User = Depends(require_role("operator")),
             db: Session = Depends(get_db)):
    render_all(db)
    return RedirectResponse("/provision", status_code=303)


# --- PXE / DHCP ---------------------------------------------------------

@router.get("/pxe")
def pxe_page(request: Request, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    dhcp = get_setting(db, "dhcp")
    conf = ""
    p = pathlib.Path(settings.dnsmasq_conf)
    if p.exists():
        conf = p.read_text()
    return templates.TemplateResponse(request, "pxe.html", {
        "user": user, "active": "pxe", "dhcp": dhcp, "conf": conf,
        "server_ip": settings.server_ip, "subnet": settings.subnet,
        "probe": request.session.pop("probe_result", None),
        "applied": request.query_params.get("applied"),
    })


@router.post("/pxe/probe")
def pxe_probe(request: Request, user: User = Depends(require_role("operator"))):
    request.session["probe_result"] = probe_dhcp()
    return RedirectResponse("/pxe", status_code=303)


@router.post("/pxe/dhcp")
def pxe_dhcp_save(request: Request, mode: str = Form(...),
                  range_start: str = Form(""), range_end: str = Form(""),
                  netmask: str = Form("255.255.255.0"), gateway: str = Form(""),
                  dns: str = Form(""), lease_time: str = Form("12h"),
                  user: User = Depends(require_role("admin")),
                  db: Session = Depends(get_db)):
    put_setting(db, "dhcp", {
        "mode": mode if mode in ("proxy", "server", "auto") else "proxy",
        "range_start": range_start.strip(), "range_end": range_end.strip(),
        "netmask": netmask.strip(), "gateway": gateway.strip(),
        "dns": dns.split(), "lease_time": lease_time.strip() or "12h",
    })
    _, effective = render_dnsmasq(db)
    ok = restart_dnsmasq()
    return RedirectResponse(f"/pxe?applied={effective}&restart={'ok' if ok else 'failed'}",
                            status_code=303)
