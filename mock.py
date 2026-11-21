#!/usr/bin/env python3
"""
Demo data generator — writes web/status.json every 2 s with fictional, moving data.
Use it to work on the design without a real server:

    MOCK_SERVICES=10 MOCK_DEVICES=8 python3 mock.py &
    python3 -m http.server 8090 --directory web

MOCK_IPV6_CHANGED=1 shows the alert raised when the host's IPv6 changes.

All names, IPs and values here are made up.
"""
import json
import math
import os
import random
import time
from datetime import datetime

HIST = 60
random.seed(7)

SERVICE_POOL = [
    ("adguardhome", "AdGuard", 80, "adguardhome:latest"),
    ("arcane", "Arcane", 3552, "arcane:latest"),
    ("dashboard", "Dashboard", 8090, "homelab-dashboard:latest"),
    ("jellyfin", "Jellyfin", 8096, "jellyfin:latest"),
    ("immich", "Immich", 2283, "immich-server:release"),
    ("vaultwarden", "Vaultwarden", 8222, "vaultwarden:latest"),
    ("nextcloud", "Nextcloud", 8443, "nextcloud:stable"),
    ("uptime-kuma", "Uptime Kuma", 3001, "uptime-kuma:2"),
    ("paperless", "Paperless", 8000, "paperless-ngx:latest"),
    ("gitea", "Gitea", 3000, "gitea:latest"),
    ("grafana", "Grafana", 3030, "grafana:latest"),
    ("homeassistant", "Home Assistant", 8123, "home-assistant:stable"),
]
DEVICE_POOL = [
    ("Router", "router"), ("Mesh AP", "ap"), ("Workstation", "computer"), ("Phone", "phone"),
    ("Smart TV", "tv"), ("Game Console", "console"), ("Smart Bulb", "bulb"), ("Laptop", "computer"),
    ("Tablet", "tablet"), ("Printer", "printer"), ("NAS", "nas"), ("Smart Speaker", "speaker"),
]

n_svc = int(os.environ.get("MOCK_SERVICES", "4"))
n_dev = int(os.environ.get("MOCK_DEVICES", "6"))
V6_CHANGED = os.environ.get("MOCK_IPV6_CHANGED") == "1"   # show the "IPv6 changed" alert
services = [dict(id=f"{SERVICE_POOL[k % len(SERVICE_POOL)][0]}{'' if k < len(SERVICE_POOL) else k}",
                 name=SERVICE_POOL[k % len(SERVICE_POOL)][1] + ("" if k < len(SERVICE_POOL) else f" {k // len(SERVICE_POOL) + 1}"), port=SERVICE_POOL[k % len(SERVICE_POOL)][2],
                 image=SERVICE_POOL[k % len(SERVICE_POOL)][3], base=2 + k * 1.3, mem=40 + k * 23)
            for k in range(n_svc)]
if n_svc >= 4:
    services[3]["down"] = True          # show one stopped container
devices = [dict(id=f"192.168.1.{[1, 2, 20, 41, 50, 60, 70, 80, 90, 100, 110, 120][k % 12] + k // 12}",
                name=DEVICE_POOL[k % len(DEVICE_POOL)][0] + ("" if k < len(DEVICE_POOL) else f" {k // len(DEVICE_POOL) + 1}"), kind=DEVICE_POOL[k % len(DEVICE_POOL)][1], base=2 + k * 3)
           for k in range(n_dev)]
if n_dev >= 5:
    devices[4]["down"] = True           # show one offline device

hist = {x["id"]: [0.0 if x.get("down") and i > HIST - 8 else
                  max(0.2, x["base"] / 10 + math.sin(i / 3 + x["base"]) * 0.4 + random.random() * 0.5)
                  for i in range(HIST)] for x in services + devices}
cpu_h = [6 + random.random() * 4 for _ in range(HIST)]
dns_h = [random.randint(80, 900) for _ in range(24)]
events = [
    ("08:00", "ok", "dashboard collector started"),
    ("08:00", "info", "network scan: %d devices" % n_dev),
    ("08:03", "info", "new device: Phone (192.168.1.41)"),
    ("08:10", "ok", "LG DevMode: Remaining: 963h 41m"),
]
if n_svc >= 4:
    events.append(("08:12", "warn", f"{services[3]['id']} exited"))
t0 = time.time()


def tick(i):
    now = datetime.now()
    for x in services + devices:
        v = 0 if x.get("down") else max(0.2, x["base"] / 10 + math.sin(i / 3 + x["base"]) * 0.4 + random.random() * 0.5)
        hist[x["id"]] = hist[x["id"]][1:] + [round(v, 2)]
    cpu_h[:] = cpu_h[1:] + [round(4 + abs(math.sin(i / 5)) * 8 + random.random() * 3, 1)]
    if i and i % 5 == 0:
        top = random.choice([("firefox", 8, 20), ("dockerd", 1, 6), ("adguardhome", 1, 4), ("python3", 2, 8)])
        events.append((now.strftime("%H:%M"), "info", f"top: {top[0]} {random.randint(top[1], top[2])}% cpu"))
    if i and i % 7 == 0:
        up = sum(1 for s in services if not s.get("down"))
        events.append((now.strftime("%H:%M"), "ok" if up == len(services) else "warn", f"{up}/{len(services)} services nominal"))
    queries = 18240 + i * 9
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "site": {"brand": os.environ.get("BRAND", "HOME"), "name": os.environ.get("NAME", "LAB"), "role": "NETOPS - homelab"},
        "weather": {"temp_c": 22, "cond": "MOSTLY CLEAR", "place": "HQ"},
        "host": {
            "name": "homelab", "ip": "192.168.1.10", "os": "NixOS 26.05", "kernel": "6.18.4",
            "uptime_s": int(93600 + time.time() - t0), "load": [round(cpu_h[-1] / 30, 2), 0.21, 0.15],
            "procs": 248 + random.randint(0, 8), "temp_c": round(42 + cpu_h[-1] / 4), "swap_pct": 0,
            "cpu": {"pct": cpu_h[-1], "threads": 12, "model": "Intel Core i5-11400H"},
            "gpus": [
                {"name": "Intel UHD Graphics", "driver": "i915", "pct": round(1 + random.random() * 3, 1), "state": "active", "vram": "shared"},
                {"name": "NVIDIA GeForce RTX 3050 Mobile", "driver": "nouveau", "pct": None, "state": "suspended", "vram": "4 GB"},
            ],
            "mem": {"used_gb": round(3.4 + random.random() * 0.2, 1), "total_gb": 31},
            "disk": {"used_gb": 212.4, "total_gb": 937, "free_gb": 724.6},
            "battery": {"pct": 100, "ac": True},
            "net": {"rx_kbps": random.randint(40, 600), "tx_kbps": random.randint(10, 150)},
        },
        "services": [
            {"id": s["id"], "name": s["name"], "state": "exited" if s.get("down") else "running",
             "uptime": "—" if s.get("down") else "1d", "url": f"http://homelab.local:{s['port']}",
             "image": s["image"], "port": s["port"],
             "cpu_pct": 0 if s.get("down") else hist[s["id"]][-1], "mem_mb": 0 if s.get("down") else s["mem"],
             "rx_kbps": 0 if s.get("down") else random.randint(1, 60), "tx_kbps": 0 if s.get("down") else random.randint(1, 40),
             "ms": None if s.get("down") else random.randint(2, 9), "hist": hist[s["id"]]}
            for s in services],
        "tasks": [{"id": "lgtv", "name": "LG DevMode", "status": "ok", "remaining_s": 963 * 3600 - (time.time() - t0),
                   "checked_at": int(t0) - 600, "msg": "Remaining: 963h 41m", "full_s": 1000 * 3600}],
        "devices": [
            {"id": d["id"], "ip": d["id"], "name": d["name"], "kind": d["kind"],
             "state": "offline" if d.get("down") else "online",
             "ms": None if d.get("down") else round(hist[d["id"]][-1] * 10), "hist": [v * 10 for v in hist[d["id"]]]}
            for d in devices],
        "dns": {"queries": queries, "blocked": int(queries * 0.17), "avg_ms": 4.2, "clients": 9,
                "hist": dns_h, "resolve_ok": True, "block_ok": True,
                "ipv6": {"expected": "2001:db8:1:2::10", "current": "2001:db8:9:8::10" if V6_CHANGED else "2001:db8:1:2::10",
                         "present": not V6_CHANGED, "answers": not V6_CHANGED, "ok": not V6_CHANGED}},
        "events": [{"t": t, "level": l, "msg": m} for t, l, m in events[-12:]],
    }


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "status.json")
    i = 0
    while True:
        with open(out + ".tmp", "w") as f:
            json.dump(tick(i), f)
        os.replace(out + ".tmp", out)
        i += 1
        time.sleep(2)
