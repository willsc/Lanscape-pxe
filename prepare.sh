#!/usr/bin/env bash
# Extract boot assets for an OS image slug from an ISO.
#
#   sudo ./prepare.sh ubuntu-24.04 /opt/foreman/.iso-cache/ubuntu-24.04-live-server-amd64.iso
#   sudo ./prepare.sh windows-11   /opt/foreman/.iso-cache/Win11_24H2_English_x64.iso
#
# Any Ubuntu release with subiquity autoinstall (22.04, 24.04, 26.04, future)
# works the same way. Slug must match an image registered in the UI.
# Also fetches iPXE binaries + wimboot on first run (copied from
# /opt/boot-install if present, so this works offline).
set -euo pipefail
cd "$(dirname "$0")"

SLUG="${1:-}"; ISO="${2:-}"
SERVER_IP="$(grep -E '^SERVER_IP=' .env 2>/dev/null | cut -d= -f2 || true)"
SERVER_IP="${SERVER_IP:-192.168.1.125}"

fetch_boot_bins() {
  mkdir -p tftp http
  for f in undionly.kpxe ipxe.efi; do
    if [[ ! -f "tftp/$f" ]]; then
      if [[ -f "/opt/boot-install/tftp/$f" ]]; then
        cp "/opt/boot-install/tftp/$f" "tftp/$f"; echo "copied $f from boot-install"
      else
        curl -fL "https://boot.ipxe.org/$f" -o "tftp/$f"; echo "downloaded $f"
      fi
    fi
  done
  if [[ ! -f http/wimboot ]]; then
    if [[ -f /opt/boot-install/http/wimboot ]]; then
      cp /opt/boot-install/http/wimboot http/wimboot; echo "copied wimboot from boot-install"
    else
      curl -fL "https://github.com/ipxe/wimboot/releases/latest/download/wimboot" -o http/wimboot
      echo "downloaded wimboot"
    fi
  fi
}

fetch_boot_bins
if [[ -z "$SLUG" ]]; then
  echo "Boot binaries ready. Usage: $0 <slug> <iso>"; exit 0
fi
[[ -f "$ISO" ]] || { echo "ISO not found: $ISO" >&2; exit 1; }

MNT=$(mktemp -d)
trap 'umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true' EXIT
mount -o loop,ro "$ISO" "$MNT"

if [[ -e "$MNT/casper/vmlinuz" ]]; then
  # ---------- Ubuntu (any release with subiquity autoinstall) ----------
  echo "==> Ubuntu image -> http/$SLUG/"
  mkdir -p "http/$SLUG"
  cp "$MNT/casper/vmlinuz" "http/$SLUG/vmlinuz"
  # initrd name varies across releases
  for c in initrd initrd.img initrd.lz; do
    [[ -e "$MNT/casper/$c" ]] && { cp "$MNT/casper/$c" "http/$SLUG/initrd"; break; }
  done
  echo "==> Copying ISO (installer fetches it over HTTP)"
  cp "$ISO" "http/$SLUG/image.iso"
  echo "Done. Ubuntu $SLUG ready."

elif [[ -e "$MNT/sources/boot.wim" ]]; then
  # ---------- Windows (wimboot + WinPE + SMB) ----------
  echo "==> Windows image -> http/$SLUG/ + smb/$SLUG/"
  mkdir -p "http/$SLUG/boot" "http/$SLUG/sources" "smb/$SLUG/drivers"
  cp "$MNT/boot/bcd"      "http/$SLUG/boot/bcd"
  cp "$MNT/boot/boot.sdi" "http/$SLUG/boot/boot.sdi"
  cp "$MNT/sources/boot.wim" "http/$SLUG/sources/boot.wim"
  echo "==> Copying full ISO contents to SMB share (this takes a while)"
  rsync -a --info=progress2 "$MNT/" "smb/$SLUG/" 2>/dev/null || cp -r "$MNT/." "smb/$SLUG/"

  echo "==> Patching boot.wim: WinPE mounts \\\\$SERVER_IP\\install and runs setup"
  STAGE=$(mktemp -d)
  mkdir -p "$STAGE/Windows/System32"
  cat > "$STAGE/Windows/System32/startnet.cmd" <<EOF
@echo off
wpeinit
wpeutil InitializeNetwork
wpeutil WaitForNetwork

REM Win11 24H2+ blocks guest SMB by default — allow it for the install share.
reg add HKLM\\SYSTEM\\CurrentControlSet\\Services\\LanmanWorkstation\\Parameters /v AllowInsecureGuestAuth /t REG_DWORD /d 1 /f
reg add HKLM\\SYSTEM\\CurrentControlSet\\Services\\LanmanWorkstation\\Parameters /v EnableInsecureGuestLogons /t REG_DWORD /d 1 /f

set /a tries=0
:mount
set /a tries=tries+1
net use Z: \\\\$SERVER_IP\\install /user:guest ""
if not errorlevel 1 goto mounted
ping -n 6 127.0.0.1 >nul
if %tries% LSS 30 goto mount
echo Mount failed after 30 attempts.
goto debug

:mounted
if exist X:\\autounattend.xml (
  REM Per-machine answer file injected by wimboot from Landscape.
  start /wait Z:\\$SLUG\\setup.exe /unattend:X:\\autounattend.xml
) else (
  start /wait Z:\\$SLUG\\setup.exe
)

:debug
echo Dropping to shell.
cmd.exe
EOF
  cat > "$STAGE/Windows/System32/winpeshl.ini" <<'EOF'
[LaunchApps]
%SYSTEMDRIVE%\Windows\System32\cmd.exe, /c %SYSTEMDRIVE%\Windows\System32\startnet.cmd
EOF

  docker run --rm -v "$PWD:/work" -v "$STAGE:/stage" debian:bookworm-slim bash -eu -c "
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends wimtools >/dev/null
    printf 'add /stage/Windows/System32/startnet.cmd /Windows/System32/startnet.cmd\nadd /stage/Windows/System32/winpeshl.ini /Windows/System32/winpeshl.ini\n' |
      wimupdate /work/http/$SLUG/sources/boot.wim 2
  "
  rm -rf "$STAGE"
  echo "Done. Windows $SLUG ready. Drop extra drivers into smb/$SLUG/drivers/."
else
  echo "Unrecognised ISO layout (no casper/ or sources/boot.wim)." >&2
  exit 1
fi
