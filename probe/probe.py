"""DHCP discovery probe.

Runs in a host-networked container so it can broadcast DHCPDISCOVER on the
LAN and read DHCPOFFER replies. Exposes one HTTP endpoint the API container
can call. Listens on 0.0.0.0:8090 — the api container reaches it via
host.docker.internal:8090 which resolves to the docker bridge gateway IP
(not the host's loopback), so we can't bind 127.0.0.1.
"""

from __future__ import annotations

import os
import random
import time
from contextlib import contextmanager

from fastapi import FastAPI
from pydantic import BaseModel
from scapy.all import BOOTP, DHCP, IP, UDP, Ether, conf, sendp, sniff

# Quiet scapy's "no route found" and similar chatter.
conf.verb = 0

app = FastAPI(title="dhcp-probe")


class ProbeResult(BaseModel):
    found: bool
    offers: list[dict]
    interface: str | None
    duration_s: float


def _pick_iface() -> str:
    # Prefer the env-provided one (set in compose). Otherwise scapy picks.
    env = os.environ.get("PROBE_IFACE")
    if env:
        return env
    # scapy returns a NetworkInterface object that stringifies to the name.
    return str(conf.iface)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/probe", response_model=ProbeResult)
def probe(timeout: int = 4):
    """Send one DHCPDISCOVER and collect any DHCPOFFERs that arrive within
    `timeout` seconds. `found=True` means there's at least one DHCP server on
    the LAN — i.e. proxy mode is the safe choice."""

    timeout = max(1, min(15, timeout))
    iface = _pick_iface()
    started = time.monotonic()

    xid = random.randint(1, 0xFFFFFFFF)
    # Use a clearly-bogus client MAC so any sensible DHCP server won't bind
    # us into its lease table (anti-pollution).
    src_mac = "02:00:00:de:ad:01"

    discover = (
        Ether(src=src_mac, dst="ff:ff:ff:ff:ff:ff")
        / IP(src="0.0.0.0", dst="255.255.255.255")
        / UDP(sport=68, dport=67)
        / BOOTP(chaddr=bytes.fromhex(src_mac.replace(":", "")), xid=xid, flags=0x8000)
        / DHCP(options=[("message-type", "discover"), "end"])
    )

    offers: list[dict] = []

    def collect(pkt):
        if BOOTP in pkt and pkt[BOOTP].xid == xid and DHCP in pkt:
            opts = dict(
                (k, v)
                for o in pkt[DHCP].options
                if isinstance(o, tuple)
                for k, v in [o]
            )
            if opts.get("message-type") == 2:  # 2 == DHCPOFFER
                offers.append(
                    {
                        "server_id": str(opts.get("server_id")),
                        "offered_ip": pkt[BOOTP].yiaddr,
                        "router": str(opts.get("router")) if opts.get("router") else None,
                        "lease_time": opts.get("lease_time"),
                    }
                )

    try:
        sendp(discover, iface=iface, verbose=0)
        sniff(
            iface=iface,
            filter="udp and (port 67 or port 68)",
            prn=collect,
            timeout=timeout,
            store=False,
        )
    except Exception as e:  # pragma: no cover — surface bad ifaces in the UI
        return ProbeResult(found=False, offers=[{"error": repr(e)}], interface=iface, duration_s=time.monotonic() - started)

    return ProbeResult(
        found=bool(offers),
        offers=offers,
        interface=iface,
        duration_s=round(time.monotonic() - started, 3),
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")
