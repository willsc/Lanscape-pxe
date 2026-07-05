# Landscape — self-hosted fleet management + bare-metal provisioning

A Landscape-style system for managing Ubuntu desktop fleets (22.04 / 24.04 /
26.04 and future releases) and Windows 11, plus PXE-based unattended installs
of both. Everything runs from docker compose on one LAN box; nothing needs
the internet after initial setup.

| Feature | How |
| --- | --- |
| Works without internet | Self-hosted UI (no CDN assets), stdlib-only agent, local apt mirror, ISO-fed installs |
| Repository management | apt-mirror sync of chosen suites, served over HTTP; one click points every host at it |
| Bring your own SSO / IAM | Any OIDC IdP (Keycloak, Authentik, Entra, Okta) + role mapping from a groups claim; local accounts as fallback |
| Software & hardware inventory | Agents report CPU/RAM/disks/NICs, full package list, pending updates every check-in |
| Compliance & reporting | Rule-based profiles (CIS-lite Ubuntu + Windows baselines built in), fleet reports, CSV exports |
| Security & hardening | One-click ufw / sshd / sysctl / unattended-upgrades / fail2ban / auditd hardening tasks |
| PXE provisioning | iPXE + subiquity autoinstall (Ubuntu), wimboot + WinPE + SMB (Windows 11) |
| DHCP flexibility | proxy (use the existing DHCP server), server (run our own), or auto (probe the LAN and decide) |

## Services

| Container | Role |
| --- | --- |
| landscape-server | FastAPI control plane + web UI (`:8443`) + agent API |
| landscape-db | Postgres |
| landscape-http | nginx: PXE assets, agent installers, apt mirror (`:8081`) |
| landscape-dnsmasq | proxy/full DHCP + TFTP (host network) |
| landscape-probe | scapy DHCPDISCOVER probe (drives auto mode / "detect" button) |
| landscape-smb | `\\<server>\install` share WinPE reads install.wim from |
| landscape-reposync | apt-mirror runner triggered from the UI |

## Quick start

```sh
cp .env.example .env        # set SERVER_IP, passwords, secrets
./prepare.sh                # stage iPXE binaries (offline if /opt/boot-install exists)
docker compose up -d --build
```

UI: `http://<SERVER_IP>:8443` — sign in with `ADMIN_USERNAME`/`ADMIN_PASSWORD`
from `.env`.

### Build OS images (in the UI)

Provisioning → click an image → **Build from ISO**. The ISO library lists
everything in `ISO_LIBRARY` (default `/opt/foreman/.iso-cache/`); builds run
in the background with a live log. Ubuntu builds extract the kernel/initrd
and stage the ISO for HTTP; Windows builds copy the media to the SMB share
and patch `boot.wim` so WinPE mounts the share and runs setup unattended
(wimboot + WinPE + SMB — the per-machine `autounattend.xml` is injected by
wimboot at boot). Any Ubuntu release with subiquity autoinstall works —
register new versions (e.g. 26.10) with a slug and build them the same way.
(`./prepare.sh <slug> <iso>` still works as a CLI alternative.)

### Customise installs

Each image has install defaults, each machine can override them:

- **Image page** — locale, keyboard, timezone, disk layout (direct/LVM),
  default username/password, extra kernel args, post-install script
  (shell / PowerShell), install from the local apt mirror (offline installs),
  auto-enroll into management; Windows adds edition and product key.
- **Machine page** — override any of the above per host, plus extra packages
  and a static IP (address/prefix/gateway/DNS). The rendered cloud-init /
  autounattend is shown at the bottom of the page for inspection.

### Provision a machine

1. **Provisioning** page → register the target's MAC, hostname, image,
   credentials, extra packages.
2. PXE-boot the target. Registered MACs install unattended; unknown MACs get
   an interactive iPXE menu. Fresh installs enroll into management on first
   boot automatically.

### DHCP modes (PXE / DHCP page)

- **proxy** — your existing DHCP keeps handing out leases; Landscape only
  adds boot hints. Safe next to a router.
- **server** — Landscape's dnsmasq serves leases (configure the range first).
- **auto** — probes the LAN at apply time and picks proxy if a DHCP server
  answers, server otherwise. The *Probe network* button shows what answered.

### Enroll existing hosts

Ubuntu:
```sh
curl -fsS http://<SERVER_IP>:8081/agent-src/install.sh | sudo sh -s -- \
  --server http://<SERVER_IP>:8443 --token <ENROLL_TOKEN>
```

Windows (admin PowerShell):
```powershell
iwr http://<SERVER_IP>:8081/agent-src/windows/install.ps1 -OutFile $env:TEMP\ls.ps1
& $env:TEMP\ls.ps1 -Server http://<SERVER_IP>:8443 -Token <ENROLL_TOKEN>
```

Tokens are managed on the Settings page. The Ubuntu agent is a single
stdlib-only Python file running as a systemd service (60 s check-in); the
Windows agent is a PowerShell scheduled task (2 min).

### Repositories / offline updates

**Repositories** page → enable the mirrors for your releases → *Sync mirrors
now* (needs internet once; ~100+ GB for full main+universe — trim suites or
components to shrink). Then *Point all Ubuntu hosts at this mirror* rewrites
every managed host's apt sources to this box. From then on installs, updates
and hardening work fully air-gapped.

### SSO / IAM

Settings → configure issuer URL, client id/secret and group→role mapping
(admin / operator / viewer). Redirect URI for your IdP:
`http://<SERVER_IP>:8443/oidc/callback`. Local accounts keep working as
break-glass.

### Compliance & hardening

- **Compliance** page: run the built-in CIS-lite Ubuntu or Windows 11
  baseline (or your own JSON rule profiles) across the fleet; export
  `compliance.csv`, `hosts.csv`, `packages.csv`.
- Host page → **Security hardening**: tick ufw / sshd / sysctl /
  unattended-upgrades / fail2ban / auditd and apply; results land in Tasks.

## Layout

```
Landscape/
├── docker-compose.yml
├── prepare.sh              # stage iPXE binaries + per-slug ISO assets
├── server/                 # FastAPI control plane + Jinja UI
├── agent/                  # agent.py (Ubuntu), windows/agent.ps1, installers
├── probe/                  # DHCP discovery sidecar
├── reposync/               # apt-mirror trigger loop
├── config/{dnsmasq,nginx}/
├── tftp/                   # iPXE binaries
├── http/                   # boot.ipxe, per-MAC configs, kernels/ISOs, mirror
├── smb/<slug>/             # Windows install media (+ drivers/)
└── repo/                   # mirror.list, status.json, mirrored packages
```

## Notes

- Default install credentials and the Windows product key placeholder are lab
  defaults — change them per machine at registration.
- `docker compose logs -f dnsmasq` shows PXE DHCP/TFTP traffic while a client
  boots.
- Windows agent supports script / compliance / reboot tasks; package
  management there is best done via scripts (winget) for now.
