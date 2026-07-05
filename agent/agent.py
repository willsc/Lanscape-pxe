#!/usr/bin/env python3
"""Landscape agent for Ubuntu hosts.

Single file, stdlib only — runs on any Ubuntu (22.04+) with no pip and no
internet access. Enrolls against the Landscape server, then loops:
check in (inventory + heartbeat), fetch queued tasks, execute, report.

Config: /etc/landscape-agent.json  {"server": ..., "host_id": ..., "agent_key": ...}
"""

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

AGENT_VERSION = "1.0"
CONF_PATH = "/etc/landscape-agent.json"
CHECKIN_INTERVAL = 60          # seconds between heartbeats
INVENTORY_EVERY = 30           # full inventory every N checkins (~30 min)
APT_ENV = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}


def sh(cmd: str, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True,
                           timeout=timeout, env=APT_ENV)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except Exception as e:
        return 1, repr(e)


def api(conf: dict, path: str, body: dict, headers: dict | None = None) -> dict:
    req = urllib.request.Request(
        conf["server"].rstrip("/") + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def agent_headers(conf: dict) -> dict:
    return {"X-Host-Id": str(conf["host_id"]), "X-Agent-Key": conf["agent_key"]}


# --- inventory ---------------------------------------------------------------

def primary_ip_mac() -> tuple[str, str]:
    ip = mac = ""
    rc, out = sh("ip -j route get 1.1.1.1 2>/dev/null")
    try:
        dev = json.loads(out)[0]["dev"]
        rc, out = sh(f"ip -j addr show {dev}")
        j = json.loads(out)[0]
        mac = j.get("address", "")
        for a in j.get("addr_info", []):
            if a.get("family") == "inet":
                ip = a["local"]
                break
    except Exception:
        pass
    return ip, mac


def hardware() -> dict:
    hw: dict = {}
    try:
        cpu, cores = "", 0
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name") and not cpu:
                cpu = line.split(":", 1)[1].strip()
            if line.startswith("processor"):
                cores += 1
        hw["cpu"], hw["cores"] = cpu, cores
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal"):
                hw["memory_mb"] = int(line.split()[1]) // 1024
                break
    except Exception:
        pass
    for key, path in (("vendor", "sys_vendor"), ("model", "product_name")):
        try:
            hw[key] = open(f"/sys/class/dmi/id/{path}").read().strip()
        except Exception:
            hw[key] = ""
    rc, out = sh("lsblk -J -d -o NAME,SIZE,TYPE 2>/dev/null")
    try:
        hw["disks"] = [{"name": d["name"], "size": d["size"]}
                       for d in json.loads(out)["blockdevices"] if d["type"] == "disk"]
    except Exception:
        hw["disks"] = []
    rc, out = sh("ip -j addr 2>/dev/null")
    try:
        nics = []
        for i in json.loads(out):
            if i.get("link_type") == "loopback":
                continue
            ip4 = next((a["local"] for a in i.get("addr_info", [])
                        if a.get("family") == "inet"), "")
            nics.append({"name": i["ifname"], "mac": i.get("address", ""), "ip": ip4})
        hw["nics"] = nics
    except Exception:
        hw["nics"] = []
    return hw


def packages() -> list:
    rc, out = sh("dpkg-query -W -f '${Package}\\t${Version}\\n' 2>/dev/null")
    pkgs = []
    for line in out.splitlines():
        if "\t" in line:
            name, ver = line.split("\t", 1)
            pkgs.append({"name": name, "version": ver})
    return pkgs


def pending_updates() -> list:
    sh("apt-get update -qq", timeout=600)
    rc, out = sh("apt-get -s -o Debug::NoLocking=1 upgrade 2>/dev/null", timeout=300)
    ups = []
    for line in out.splitlines():
        if line.startswith("Inst "):
            parts = line.split()
            ups.append({"name": parts[1],
                        "version": parts[3].strip("(") if len(parts) > 3 else "",
                        "security": "-security" in line})
    return ups


def os_release() -> tuple[str, str]:
    info = {}
    try:
        for line in open("/etc/os-release"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                info[k] = v.strip('"')
    except Exception:
        pass
    return info.get("ID", "ubuntu"), info.get("VERSION_ID", "")


def inventory(full: bool) -> dict:
    ip, mac = primary_ip_mac()
    _, ver = os_release()
    inv = {
        "hostname": socket.gethostname(),
        "os_version": ver,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "ip": ip, "mac": mac,
        "agent_version": AGENT_VERSION,
        "reboot_required": os.path.exists("/var/run/reboot-required"),
    }
    if full:
        inv["hardware"] = hardware()
        inv["packages"] = packages()
        inv["updates"] = pending_updates()
    return inv


# --- compliance --------------------------------------------------------------

def check_rule(rule: dict) -> dict:
    t = rule.get("type")
    ok, detail = False, ""
    try:
        if t == "sshd_config":
            rc, out = sh("sshd -T 2>/dev/null")
            key = rule["key"].lower()
            val = next((l.split(None, 1)[1] for l in out.splitlines()
                        if l.split(None, 1)[0] == key and len(l.split(None, 1)) > 1), None)
            detail = f"{key}={val}"
            if val is None:
                ok = False
            elif "expect" in rule:
                ok = val.strip().lower() == str(rule["expect"]).lower()
            elif "expect_max" in rule:
                ok = val.strip().isdigit() and int(val) <= int(rule["expect_max"])
        elif t == "package_installed":
            rc, _ = sh(f"dpkg -s {rule['package']} >/dev/null 2>&1")
            ok = rc == 0
        elif t == "package_absent":
            rc, _ = sh(f"dpkg -s {rule['package']} >/dev/null 2>&1")
            ok = rc != 0
        elif t == "sysctl":
            rc, out = sh(f"sysctl -n {rule['key']} 2>/dev/null")
            detail = out.strip()
            ok = out.strip() == str(rule["expect"])
        elif t == "command":
            rc, out = sh(rule["cmd"], timeout=60)
            detail = out.strip()[:300]
            ok = rc == int(rule.get("expect_rc", 0))
        elif t == "no_pending_security_updates":
            ups = pending_updates()
            sec = [u["name"] for u in ups if u["security"]]
            detail = ", ".join(sec[:10])
            ok = not sec
        elif t == "reboot_not_required":
            ok = not os.path.exists("/var/run/reboot-required")
        else:
            detail = f"unknown rule type {t}"
    except Exception as e:
        detail = repr(e)
    return {"id": rule.get("id"), "desc": rule.get("desc"), "ok": ok, "detail": detail}


# --- hardening ---------------------------------------------------------------

SSH_HARDEN = """# Managed by Landscape
PermitRootLogin no
MaxAuthTries 3
X11Forwarding no
ClientAliveInterval 300
ClientAliveCountMax 3
"""

SYSCTL_HARDEN = """# Managed by Landscape
kernel.randomize_va_space = 2
net.ipv4.ip_forward = 0
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.all.accept_redirects = 0
net.ipv4.conf.all.accept_source_route = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.tcp_syncookies = 1
"""

AUTO_UPGRADES = """APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
"""


def harden(actions: list) -> tuple[int, str]:
    log, rc_total = [], 0

    def run(desc, cmd):
        nonlocal rc_total
        rc, out = sh(cmd)
        log.append(f"[{'ok' if rc == 0 else 'FAIL'}] {desc}\n{out.strip()}")
        rc_total |= rc

    if "firewall" in actions:
        run("install ufw", "apt-get install -y -qq ufw")
        run("ufw policy + ssh", "ufw default deny incoming && ufw default allow outgoing && ufw allow ssh")
        run("ufw enable", "ufw --force enable")
    if "ssh" in actions:
        os.makedirs("/etc/ssh/sshd_config.d", exist_ok=True)
        open("/etc/ssh/sshd_config.d/60-landscape.conf", "w").write(SSH_HARDEN)
        run("reload sshd", "sshd -t && (systemctl reload ssh || systemctl reload sshd)")
    if "sysctl" in actions:
        open("/etc/sysctl.d/60-landscape.conf", "w").write(SYSCTL_HARDEN)
        run("apply sysctl", "sysctl --system >/dev/null && echo applied")
    if "auto_updates" in actions:
        run("install unattended-upgrades", "apt-get install -y -qq unattended-upgrades")
        open("/etc/apt/apt.conf.d/20auto-upgrades", "w").write(AUTO_UPGRADES)
        log.append("[ok] enabled periodic unattended upgrades")
    if "fail2ban" in actions:
        run("install fail2ban", "apt-get install -y -qq fail2ban")
        run("enable fail2ban", "systemctl enable --now fail2ban")
    if "auditd" in actions:
        run("install auditd", "apt-get install -y -qq auditd")
        run("enable auditd", "systemctl enable --now auditd")
    return rc_total, "\n".join(log)


# --- task execution ----------------------------------------------------------

def run_task(task: dict) -> tuple[str, int, str]:
    t, p = task["type"], task.get("payload") or {}
    if t == "pkg_install":
        rc, out = sh("apt-get install -y -qq " + " ".join(p.get("packages", [])))
    elif t == "pkg_remove":
        rc, out = sh("apt-get remove -y -qq " + " ".join(p.get("packages", [])))
    elif t == "upgrade":
        rc, out = sh("apt-get update -qq && apt-get upgrade -y -qq", timeout=3600)
    elif t == "script":
        rc, out = sh(p.get("script", ""), timeout=1800)
    elif t == "harden":
        rc, out = harden(p.get("actions", []))
    elif t == "compliance_scan":
        results = [check_rule(r) for r in p.get("rules", [])]
        return "done", 0, json.dumps(results)
    elif t == "repo_config":
        open("/etc/apt/sources.list.d/landscape-mirror.list", "w").write(p.get("sources", ""))
        rc, out = sh("apt-get update", timeout=600)
    elif t == "reboot":
        sh("shutdown -r +1 'Landscape-requested reboot'")
        return "done", 0, "reboot scheduled in 1 minute"
    else:
        return "failed", 1, f"unknown task type {t}"
    return ("done" if rc == 0 else "failed"), rc, out


# --- main loop ---------------------------------------------------------------

def enroll(server: str, token: str) -> dict:
    machine_id = open("/etc/machine-id").read().strip()
    ip, mac = primary_ip_mac()
    fam, ver = os_release()
    resp = api({"server": server}, "/agent/enroll", {
        "token": token, "machine_id": machine_id, "hostname": socket.gethostname(),
        "os_family": "ubuntu", "os_version": ver, "kernel": platform.release(),
        "arch": platform.machine(), "ip": ip, "mac": mac,
        "agent_version": AGENT_VERSION,
    })
    conf = {"server": server, "host_id": resp["host_id"], "agent_key": resp["agent_key"]}
    with open(CONF_PATH, "w") as f:
        json.dump(conf, f)
    os.chmod(CONF_PATH, 0o600)
    print(f"enrolled as host {resp['host_id']}")
    return conf


def loop(conf: dict):
    n = 0
    while True:
        try:
            full = n % INVENTORY_EVERY == 0
            resp = api(conf, "/agent/checkin", inventory(full), agent_headers(conf))
            for task in resp.get("tasks", []):
                status, rc, out = run_task(task)
                api(conf, "/agent/result",
                    {"task_id": task["id"], "status": status, "exit_code": rc,
                     "output": out}, agent_headers(conf))
                if task["type"] in ("pkg_install", "pkg_remove", "upgrade", "harden"):
                    n = 0  # force fresh inventory next checkin
                    n -= 1
        except urllib.error.URLError as e:
            print(f"server unreachable: {e}", file=sys.stderr)
        except Exception as e:
            print(f"checkin error: {e!r}", file=sys.stderr)
        n += 1
        time.sleep(CHECKIN_INTERVAL)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enroll", action="store_true")
    ap.add_argument("--server")
    ap.add_argument("--token")
    args = ap.parse_args()

    if args.enroll:
        if not (args.server and args.token):
            sys.exit("--enroll needs --server and --token")
        enroll(args.server, args.token)
        return

    if not os.path.exists(CONF_PATH):
        sys.exit(f"not enrolled ({CONF_PATH} missing) — run with --enroll first")
    conf = json.load(open(CONF_PATH))
    loop(conf)


if __name__ == "__main__":
    main()
