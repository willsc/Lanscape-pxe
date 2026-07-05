"""In-UI image building: extract boot assets from an ISO in the ISO library
and (for Windows) patch boot.wim so WinPE mounts the SMB share and runs
setup unattended.

Runs in a background thread; progress streams into the jobs table so the UI
can poll the log. Uses 7z for extraction (no loop mounts needed, so it works
inside the unprivileged server container) and wimlib for boot.wim edits.
"""

import os
import pathlib
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone

from .config import settings
from .db import SessionLocal
from .models import Image, Job

ISO_DIR = os.environ.get("ISO_DIR", "/srv/isos")

STARTNET_TEMPLATE = r"""@echo off
wpeinit
wpeutil InitializeNetwork
wpeutil WaitForNetwork

REM Win11 24H2+ blocks guest SMB by default - allow it for the install share.
reg add HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters /v AllowInsecureGuestAuth /t REG_DWORD /d 1 /f
reg add HKLM\SYSTEM\CurrentControlSet\Services\LanmanWorkstation\Parameters /v EnableInsecureGuestLogons /t REG_DWORD /d 1 /f

set /a tries=0
:mount
set /a tries=tries+1
net use Z: \\{server_ip}\install /user:guest ""
if not errorlevel 1 goto mounted
ping -n 6 127.0.0.1 >nul
if %tries% LSS 30 goto mount
echo Mount failed after 30 attempts.
goto debug

:mounted
if exist X:\autounattend.xml (
  REM Per-machine answer file injected by wimboot from Landscape.
  start /wait Z:\{slug}\setup.exe /unattend:X:\autounattend.xml
) else (
  start /wait Z:\{slug}\setup.exe
)

:debug
echo Dropping to shell.
cmd.exe
"""

WINPESHL_INI = ("[LaunchApps]\r\n"
                "%SYSTEMDRIVE%\\Windows\\System32\\cmd.exe, "
                "/c %SYSTEMDRIVE%\\Windows\\System32\\startnet.cmd\r\n")


def list_isos() -> list[dict]:
    root = pathlib.Path(ISO_DIR)
    out = []
    if root.is_dir():
        for p in sorted(root.glob("*.iso")):
            st = p.stat()
            if st.st_size < 50_000_000:  # skip placeholders / partial downloads
                continue
            out.append({"name": p.name, "size_gb": round(st.st_size / 1e9, 2),
                        "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")})
    return out


def _append(db, job: Job, line: str) -> None:
    job.log = (job.log or "") + line.rstrip() + "\n"
    db.commit()


def _run(db, job: Job, cmd: list[str], desc: str, quiet: bool = False) -> None:
    _append(db, job, f"==> {desc}")
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        _append(db, job, (p.stdout + p.stderr)[-4000:])
        raise RuntimeError(f"{desc} failed (rc={p.returncode})")
    if not quiet and (p.stdout or p.stderr).strip():
        _append(db, job, (p.stdout + p.stderr).strip()[-1500:])


def _seven_zip_list(iso: str) -> list[str]:
    p = subprocess.run(["7z", "l", "-ba", "-slt", iso], capture_output=True, text=True)
    return re.findall(r"^Path = (.+)$", p.stdout, re.M)


def _build_ubuntu(db, job: Job, img: Image, iso: str) -> None:
    dest = pathlib.Path(settings.http_dir) / img.slug
    dest.mkdir(parents=True, exist_ok=True)
    names = _seven_zip_list(iso)
    by_lower = {n.lower(): n for n in names}

    def pick(*cands):  # first candidate present wins — order = preference
        return next((by_lower[c] for c in cands if c in by_lower), None)

    kernel = pick("casper/vmlinuz", "casper/hwe-vmlinuz")
    initrd = pick("casper/initrd", "casper/initrd.img", "casper/initrd.lz",
                  "casper/hwe-initrd")
    if not kernel or not initrd:
        raise RuntimeError("ISO has no casper/vmlinuz+initrd — is this a live/live-server ISO?")
    _run(db, job, ["7z", "e", "-y", f"-o{dest}", iso, kernel, initrd],
         f"extract {kernel} + {initrd}", quiet=True)
    (dest / pathlib.Path(kernel).name).rename(dest / "vmlinuz")
    (dest / pathlib.Path(initrd).name).rename(dest / "initrd")
    _append(db, job, "==> copy ISO for HTTP-served install (this is the big step)")
    shutil.copyfile(iso, dest / "image.iso")
    _append(db, job, f"copied {os.path.getsize(iso) / 1e9:.2f} GB")


def _build_windows(db, job: Job, img: Image, iso: str) -> None:
    http_dest = pathlib.Path(settings.http_dir) / img.slug
    smb_dest = pathlib.Path(settings.smb_dir) / img.slug
    (http_dest / "boot").mkdir(parents=True, exist_ok=True)
    (http_dest / "sources").mkdir(parents=True, exist_ok=True)
    smb_dest.mkdir(parents=True, exist_ok=True)

    names = {n.lower(): n for n in _seven_zip_list(iso)}
    for want, sub in (("boot/bcd", "boot"), ("boot/boot.sdi", "boot"),
                      ("sources/boot.wim", "sources")):
        real = names.get(want)
        if not real:
            raise RuntimeError(f"ISO missing {want} — not a Windows install ISO?")
        _run(db, job, ["7z", "e", "-y", f"-o{http_dest / sub}", iso, real],
             f"extract {real}", quiet=True)
        got = http_dest / sub / pathlib.Path(real).name
        want_name = pathlib.Path(want).name
        if got.name != want_name:
            got.rename(http_dest / sub / want_name)
    # normalise BCD name to lowercase (template references boot/bcd)
    for f in (http_dest / "boot").iterdir():
        if f.name.lower() == "bcd" and f.name != "bcd":
            f.rename(http_dest / "boot" / "bcd")

    _append(db, job, "==> extract full ISO to SMB share (several GB, takes a while)")
    _run(db, job, ["7z", "x", "-y", f"-o{smb_dest}", iso], "7z extract to SMB", quiet=True)
    (smb_dest / "drivers").mkdir(exist_ok=True)
    _append(db, job, f"SMB tree ready at smb/{img.slug}/ (drop drivers into drivers/)")

    _append(db, job, "==> patch boot.wim: WinPE mounts SMB + runs setup unattended")
    stage = pathlib.Path("/tmp/wimstage")
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "Windows/System32").mkdir(parents=True)
    (stage / "Windows/System32/startnet.cmd").write_text(
        STARTNET_TEMPLATE.format(server_ip=settings.server_ip, slug=img.slug))
    (stage / "Windows/System32/winpeshl.ini").write_text(WINPESHL_INI)
    cmds = (f"add {stage}/Windows/System32/startnet.cmd /Windows/System32/startnet.cmd\n"
            f"add {stage}/Windows/System32/winpeshl.ini /Windows/System32/winpeshl.ini\n")
    p = subprocess.run(["wimupdate", str(http_dest / "sources/boot.wim"), "2"],
                       input=cmds, capture_output=True, text=True)
    if p.returncode != 0:
        _append(db, job, (p.stdout + p.stderr)[-3000:])
        raise RuntimeError("wimupdate failed")
    shutil.rmtree(stage, ignore_errors=True)
    _append(db, job, "boot.wim patched")


def _build(job_id: int, image_id: int, iso_name: str) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        img = db.get(Image, image_id)
        iso = str(pathlib.Path(ISO_DIR) / iso_name)
        try:
            if "/" in iso_name or not os.path.isfile(iso):
                raise RuntimeError(f"ISO not found: {iso_name}")
            _append(db, job, f"Building image '{img.name}' (slug {img.slug}) from {iso_name}")
            if img.os_family == "ubuntu":
                _build_ubuntu(db, job, img, iso)
            else:
                _build_windows(db, job, img, iso)
            img.iso_path = iso_name
            job.status = "done"
            _append(db, job, "Build complete — image is ready to install.")
        except Exception as e:
            job.status = "failed"
            _append(db, job, f"FAILED: {e}")
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()


def start_build(db, image: Image, iso_name: str) -> Job:
    job = Job(kind="image_build", ref_id=image.id,
              title=f"build {image.slug} from {iso_name}")
    db.add(job)
    db.commit()
    threading.Thread(target=_build, args=(job.id, image.id, iso_name),
                     daemon=True).start()
    return job


def build_running(db, image_id: int) -> bool:
    from sqlalchemy import select
    return db.scalar(select(Job).where(Job.ref_id == image_id,
                                       Job.kind == "image_build",
                                       Job.status == "running")) is not None
