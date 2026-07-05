"""First-boot seeding: admin user, DHCP defaults, builtin compliance
profiles, starter repo mirrors and image slots."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import hash_password
from .config import settings
from .models import ComplianceProfile, Image, RepoMirror, Setting, User

CIS_LITE_UBUNTU = [
    {"id": "ssh-no-root", "desc": "SSH: root login disabled", "type": "sshd_config",
     "key": "PermitRootLogin", "expect": "no"},
    {"id": "ssh-max-auth", "desc": "SSH: MaxAuthTries <= 4", "type": "sshd_config",
     "key": "MaxAuthTries", "expect_max": 4},
    {"id": "ufw-installed", "desc": "Firewall (ufw) installed", "type": "package_installed",
     "package": "ufw"},
    {"id": "ufw-active", "desc": "Firewall (ufw) active", "type": "command",
     "cmd": "ufw status | grep -q 'Status: active'", "expect_rc": 0},
    {"id": "auto-updates", "desc": "unattended-upgrades installed", "type": "package_installed",
     "package": "unattended-upgrades"},
    {"id": "no-telnet", "desc": "telnet server absent", "type": "package_absent",
     "package": "telnetd"},
    {"id": "no-rsh", "desc": "rsh server absent", "type": "package_absent",
     "package": "rsh-server"},
    {"id": "aslr", "desc": "ASLR enabled (kernel.randomize_va_space=2)", "type": "sysctl",
     "key": "kernel.randomize_va_space", "expect": "2"},
    {"id": "ip-forward", "desc": "IPv4 forwarding disabled", "type": "sysctl",
     "key": "net.ipv4.ip_forward", "expect": "0"},
    {"id": "no-sec-updates", "desc": "No pending security updates", "type": "no_pending_security_updates"},
    {"id": "no-reboot-req", "desc": "No reboot required", "type": "reboot_not_required"},
    {"id": "shadow-perms", "desc": "/etc/shadow not world-readable", "type": "command",
     "cmd": "stat -c %a /etc/shadow | grep -qE '^(0|6[04]0)$'", "expect_rc": 0},
]

BASELINE_WINDOWS = [
    {"id": "defender-on", "desc": "Defender real-time protection on", "type": "powershell",
     "cmd": "if ((Get-MpComputerStatus).RealTimeProtectionEnabled) { exit 0 } else { exit 1 }"},
    {"id": "firewall-on", "desc": "Windows Firewall enabled (all profiles)", "type": "powershell",
     "cmd": "if ((Get-NetFirewallProfile | Where-Object {-not $_.Enabled}).Count -eq 0) { exit 0 } else { exit 1 }"},
    {"id": "uac-on", "desc": "UAC enabled", "type": "powershell",
     "cmd": "if ((Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Policies\\System').EnableLUA -eq 1) { exit 0 } else { exit 1 }"},
    {"id": "smb1-off", "desc": "SMBv1 disabled", "type": "powershell",
     "cmd": "if ((Get-WindowsOptionalFeature -Online -FeatureName SMB1Protocol).State -ne 'Enabled') { exit 0 } else { exit 1 }"},
]

DEFAULT_IMAGES = [
    {"name": "Ubuntu 22.04 LTS Desktop", "os_family": "ubuntu", "version": "22.04",
     "slug": "ubuntu-22.04", "default_packages": ["ubuntu-desktop-minimal", "openssh-server"]},
    {"name": "Ubuntu 24.04 LTS Desktop", "os_family": "ubuntu", "version": "24.04",
     "slug": "ubuntu-24.04", "default_packages": ["ubuntu-desktop-minimal", "openssh-server"]},
    {"name": "Ubuntu 26.04 LTS Desktop", "os_family": "ubuntu", "version": "26.04",
     "slug": "ubuntu-26.04", "default_packages": ["ubuntu-desktop-minimal", "openssh-server"]},
    {"name": "Windows 11", "os_family": "windows", "version": "11", "slug": "windows-11",
     "default_packages": []},
]

DEFAULT_MIRRORS = [
    {"name": "ubuntu-noble", "upstream": "http://archive.ubuntu.com/ubuntu",
     "suites": ["noble", "noble-updates", "noble-security"],
     "components": ["main", "universe"], "arches": ["amd64"], "enabled": False},
    {"name": "ubuntu-jammy", "upstream": "http://archive.ubuntu.com/ubuntu",
     "suites": ["jammy", "jammy-updates", "jammy-security"],
     "components": ["main", "universe"], "arches": ["amd64"], "enabled": False},
]


def seed(db: Session) -> None:
    if not db.scalar(select(User).where(User.username == settings.admin_username)):
        db.add(User(username=settings.admin_username,
                    pw_hash=hash_password(settings.admin_password),
                    role="admin", source="local"))

    if not db.get(Setting, "dhcp"):
        db.add(Setting(key="dhcp", value={
            "mode": "proxy",  # proxy | server | auto
            "range_start": "", "range_end": "", "netmask": "255.255.255.0",
            "gateway": "", "dns": [], "lease_time": "12h",
        }))
    if not db.get(Setting, "oidc"):
        db.add(Setting(key="oidc", value={}))

    if not db.scalar(select(ComplianceProfile).where(ComplianceProfile.name == "CIS-lite Ubuntu baseline")):
        db.add(ComplianceProfile(name="CIS-lite Ubuntu baseline", os_family="ubuntu",
                                 description="Pragmatic subset of CIS Ubuntu benchmarks.",
                                 rules=CIS_LITE_UBUNTU, builtin=True))
    if not db.scalar(select(ComplianceProfile).where(ComplianceProfile.name == "Windows 11 baseline")):
        db.add(ComplianceProfile(name="Windows 11 baseline", os_family="windows",
                                 description="Defender, firewall, UAC, SMBv1.",
                                 rules=BASELINE_WINDOWS, builtin=True))

    for img in DEFAULT_IMAGES:
        if not db.scalar(select(Image).where(Image.slug == img["slug"])):
            db.add(Image(**img))
    for m in DEFAULT_MIRRORS:
        if not db.scalar(select(RepoMirror).where(RepoMirror.name == m["name"])):
            db.add(RepoMirror(**m))

    db.commit()
