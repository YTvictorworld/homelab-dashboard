#!/usr/bin/env python3
"""
Homelab terminal dashboard — data collector + web server.

Collects the state of the homelab and serves it as /status.json next to the page (web/).
Python standard library only.

Modes:
  * local (production, inside the container on the server):
      reads /proc and /sys directly and Docker through /var/run/docker.sock.
  * ssh (testing from another machine):  SSH_TARGET=user@host
      reads the server's /proc and /sys over SSH and Docker through an SSH-tunnelled socket.

Everything is configured with environment variables (see CONFIG below).
"""
import http.client
import http.server
import ipaddress
import json
import os
import random
import re
import shlex
import socket
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from base64 import b64encode
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))


# ── Configuration ──────────────────────────────────────────────────────────
def dur(s, default):
    """'30s' / '5m' / '6h' / '7d' -> segundos."""
    s = (s or "").strip().lower()
    if not s:
        return default
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", s)
    if not m:
        return default
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def load_dotenv(path):
    """Load KEY=VALUE pairs from a .env file without overriding existing variables."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    except OSError:
        pass


load_dotenv(os.path.join(HERE, ".env"))
E = os.environ.get
CONFIG = {
    "bind": E("BIND", "0.0.0.0"),
    "port": int(E("PORT", "8090")),
    "ssh_target": E("SSH_TARGET", ""),               # empty = local mode
    "docker_sock": E("DOCKER_SOCK", "/var/run/docker.sock"),
    "host_ip": E("HOST_IP", ""),                      # auto-detected when empty
    "lan_cidr": E("LAN_CIDR", ""),                    # auto-detected when empty
    "brand": E("BRAND", "HOME"),
    "name": E("NAME", "LAB"),
    "role": E("ROLE", "NETOPS - homelab"),
    "adguard_url": E("ADGUARD_URL", "http://127.0.0.1:80"),
    "adguard_user": E("ADGUARD_USER", ""),
    "adguard_pass": E("ADGUARD_PASSWORD", ""),
    "dns_server": E("DNS_SERVER", ""),                # default: HOST_IP
    "dns_ipv6": E("DNS_IPV6", ""),                    # IPv6 DNS set in the router (check is off when empty)
    "lat": E("WEATHER_LAT", ""),                      # weather is off unless both are set
    "lon": E("WEATHER_LON", ""),
    "place": E("WEATHER_PLACE", ""),                  # short label, e.g. "NYC"
    "state_file": E("STATE_FILE", os.path.join(HERE, "data", "state.json")),
    "names_file": E("NAMES_FILE", os.path.join(HERE, "data", "names.json")),
    "lgtv_file": E("LGTV_FILE", os.path.join(HERE, "data", "lgtv.json")),
    "public_host": E("PUBLIC_HOST", ""),              # e.g. myserver.local (default: <hostname>.local)
    # intervals
    "t_fast": dur(E("SYNC_INTERVAL"), 3),             # Docker + host
    "t_latency": dur(E("LATENCY_INTERVAL"), 10),      # HTTP to services + DNS checks
    "t_ping": dur(E("PING_INTERVAL"), 30),            # known devices
    "t_scan": dur(E("SCAN_INTERVAL"), 6 * 3600),      # diskver new devices
    "t_weather": dur(E("WEATHER_INTERVAL"), 15 * 60),
    "forget_after": dur(E("CLEANUP_AFTER"), 7 * 86400),
    "summary_every": dur(E("SUMMARY_INTERVAL"), 5 * 60),
}
HIST = 60  # points in each sparkline
UNKNOWN = "Unknown Device"  # name of a device that does not announce one


# ── Shared state ──────────────────────────────────────────────────────
LOCK = threading.RLock()
STATE = {"host": {}, "services": [], "devices": [], "dns": None, "weather": None, "tasks": []}
EVENTS = deque(maxlen=60)
SVC_HIST = {}          # name -> deque of cpu%
DEV = {}               # ip -> persistent device dict
LAT = {}               # service name -> ms


def event(level, msg):
    with LOCK:
        EVENTS.append({"t": datetime.now().strftime("%H:%M"), "level": level, "msg": msg})
    print(f"[{level.upper()}] {msg}", flush=True)


def log_err(where, e):
    print(f"[error] {where}: {e}", flush=True)


# ── Host access (local or over SSH) ───────────────────────────────────────
HOST_FILES = [
    "/proc/stat", "/proc/meminfo", "/proc/loadavg", "/proc/uptime", "/proc/net/dev",
    "/proc/sys/kernel/osrelease", E("OS_RELEASE", "/etc/os-release"), "/proc/sys/kernel/hostname",
]
HOST_GLOBS = [
    "/sys/class/power_supply/*/capacity", "/sys/class/power_supply/*/status",
    "/sys/class/power_supply/*/online", "/sys/class/thermal/thermal_zone*/type",
    "/sys/class/thermal/thermal_zone*/temp",
    "/sys/class/drm/card[0-9]/device/resource", "/sys/class/drm/card[0-9]/device/power/runtime_status",
    "/sys/class/drm/card[0-9]/gt/gt0/rc6_residency_ms",
]
HOST_CMDS = {
    "df": "df -Pk / | tail -1 | awk '{print $2*1024, $3*1024}'",   # works with GNU and busybox df
    "procs": "ls -d /proc/[0-9]* | wc -l",
    "top": "ps -eo comm,pcpu --sort=-pcpu --no-headers | head -1",
    "route": "ip route show default | head -1",
    "ip6": "ip -6 -o addr show scope global 2>/dev/null",
    "cpumodel": "grep -m1 'model name' /proc/cpuinfo",
    "gpu": "lspci 2>/dev/null | grep -Ei 'vga|3d controller|display'",
    "gpudrv": "for c in /sys/class/drm/card[0-9]; do echo $(basename $c) $(basename $(readlink -f $c/device)) "
              "$(basename $(readlink -f $c/device/driver)); done",
}


def ssh_base():
    ctl = os.path.join(tempfile.gettempdir(), "homelab-dash-ssh-%r@%h")
    return ["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=auto",
            "-o", f"ControlPath={ctl}", "-o", "ControlPersist=120", CONFIG["ssh_target"]]


def host_dump():
    """Return {path_or_command: content} from the host."""
    out = {}
    if CONFIG["ssh_target"]:
        parts = [f'for f in {" ".join(HOST_FILES)} {" ".join(HOST_GLOBS)}; do '
                 f'[ -r "$f" ] && {{ echo "=== $f"; cat "$f"; }}; done']
        for k, c in HOST_CMDS.items():
            parts.append(f'echo "=== cmd:{k}"; {c}')
        r = subprocess.run(ssh_base() + ["; ".join(parts)], capture_output=True, text=True, timeout=20)
        key = None
        for line in r.stdout.splitlines():
            if line.startswith("=== "):
                key = line[4:]
                out[key] = ""
            elif key is not None:
                out[key] += line + "\n"
        return out
    import glob
    for f in HOST_FILES + [g for pat in HOST_GLOBS for g in glob.glob(pat)]:
        try:
            with open(f) as fh:
                out[f] = fh.read()
        except OSError:
            pass
    for k, c in HOST_CMDS.items():
        try:
            out[f"cmd:{k}"] = subprocess.run(["sh", "-c", c], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            pass
    return out


# ── Docker Engine API over a UNIX socket ────────────────────────────────────
class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


_tunnel = {"proc": None, "sock": None}


def docker_sock():
    if not CONFIG["ssh_target"]:
        return CONFIG["docker_sock"]
    p = _tunnel["proc"]
    if p is None or p.poll() is not None:
        sock = os.path.join(tempfile.gettempdir(), f"homelab-dash-docker-{os.getpid()}.sock")
        if os.path.exists(sock):
            os.unlink(sock)
        _tunnel["proc"] = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-N",
             "-L", f"{sock}:/var/run/docker.sock", CONFIG["ssh_target"]],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _tunnel["sock"] = sock
        for _ in range(50):
            if os.path.exists(sock):
                break
            time.sleep(0.1)
    return _tunnel["sock"]


def docker_get(path):
    c = UnixHTTPConnection(docker_sock())
    try:
        c.request("GET", path)
        r = c.getresponse()
        data = r.read()
        if r.status != 200:
            raise RuntimeError(f"docker {path}: HTTP {r.status}")
        return json.loads(data)
    finally:
        c.close()


# ── Utilities ─────────────────────────────────────────────────────────────
def short_uptime(status):
    """'Up 5 hours (healthy)' -> '5h'."""
    m = re.match(r"Up (?:(About an?|\d+) )?(\w+)", status or "")
    if not m:
        return "—"
    n, unit = m.group(1), m.group(2)
    n = "1" if n and n.startswith("About") else (n or "1")
    if unit.startswith("second") or unit == "Less":
        return "<1m"
    return n + {"minute": "m", "hour": "h", "day": "d", "week": "w", "month": "mo", "year": "y"}.get(unit.rstrip("s"), "")


def pretty(name):
    return name.replace("-", " ").replace("_", " ").title()


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def names():
    """Optional data/names.json: {"services": {"adguardhome": {"name": "AdGuard", "port": 80}},
                                  "devices": {"192.168.1.1": {"name": "Router", "kind": "router"}}}"""
    return load_json(CONFIG["names_file"], {})


# ── Collector: host ─────────────────────────────────────────────────────────
_prev_cpu = {}
_prev_net = {}
_prev_gpu = {}


def collect_host():
    d = host_dump()
    h = {}
    # CPU % (difference between two /proc/stat samples)
    line = (d.get("/proc/stat") or "").splitlines()[0:1]
    if line:
        v = list(map(int, line[0].split()[1:]))
        idle, total = v[3] + v[4], sum(v[:8])
        if _prev_cpu:
            dt = total - _prev_cpu["total"]
            h["cpu_pct"] = round(100 * (1 - (idle - _prev_cpu["idle"]) / dt), 1) if dt > 0 else 0.0
        _prev_cpu.update(idle=idle, total=total)
        h["threads"] = sum(1 for l in d["/proc/stat"].splitlines() if re.match(r"cpu\d", l))
    mi = {k: int(v.split()[0]) * 1024 for k, v in
          (l.split(":", 1) for l in (d.get("/proc/meminfo") or "").splitlines() if ":" in l)}
    if mi:
        h["mem_total"] = mi.get("MemTotal", 0)
        h["mem_used"] = mi.get("MemTotal", 0) - mi.get("MemAvailable", 0)
        st = mi.get("SwapTotal", 0)
        h["swap_pct"] = round(100 * (st - mi.get("SwapFree", 0)) / st) if st else 0
    la = (d.get("/proc/loadavg") or "0 0 0").split()
    h["load"] = [float(x) for x in la[:3]]
    h["uptime_s"] = int(float((d.get("/proc/uptime") or "0").split()[0]))
    h["kernel"] = (d.get("/proc/sys/kernel/osrelease") or "").strip()
    h["hostname"] = E("HOST_NAME") or (d.get("/proc/sys/kernel/hostname") or "").strip()
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', d.get(E("OS_RELEASE", "/etc/os-release")) or "", re.M))
    h["os"] = osr.get("PRETTY_NAME", "Linux").replace(" (", " ").rstrip(")")
    if osr.get("NAME") == "NixOS":
        h["os"] = f"NixOS {osr.get('VERSION_ID', '')}".strip()
    # network: interface of the default route
    m = re.search(r"dev (\S+)", d.get("cmd:route") or "")
    iface = m.group(1) if m else None
    now = time.time()
    for l in (d.get("/proc/net/dev") or "").splitlines()[2:]:
        name, rest = l.split(":", 1)
        if name.strip() == iface:
            f = rest.split()
            rx, tx = int(f[0]), int(f[8])
            if _prev_net:
                dt = now - _prev_net["t"]
                h["rx_kbps"] = round((rx - _prev_net["rx"]) / dt / 1000, 1)
                h["tx_kbps"] = round((tx - _prev_net["tx"]) / dt / 1000, 1)
            _prev_net.update(rx=rx, tx=tx, t=now)
    h["iface"] = iface
    # disk
    df = (d.get("cmd:df") or "").split()
    if len(df) == 2:
        h["disk_total"], h["disk_used"] = int(df[0]), int(df[1])
    # battery / charger
    for k, v in d.items():
        if re.match(r"/sys/class/power_supply/BAT\w*/capacity", k):
            h["battery_pct"] = int(v.strip())
        elif re.match(r"/sys/class/power_supply/BAT\w*/status", k):
            h["battery_status"] = v.strip()
        elif re.match(r"/sys/class/power_supply/(ADP|AC)\w*/online", k):
            h["ac"] = v.strip() == "1"
    # CPU temperature
    zones = {}
    for k, v in d.items():
        m = re.match(r"(/sys/class/thermal/thermal_zone\d+)/(type|temp)", k)
        if m:
            zones.setdefault(m.group(1), {})[m.group(2)] = v.strip()
    temps = {z.get("type"): int(z["temp"]) / 1000 for z in zones.values() if z.get("temp", "").lstrip("-").isdigit()}
    h["temp_c"] = round(temps.get("x86_pkg_temp") or temps.get("acpitz") or max(temps.values(), default=0))
    h["procs"] = int((d.get("cmd:procs") or "0").strip() or 0)
    # CPU and GPU model names (cleaned up for display)
    cm = (d.get("cmd:cpumodel") or "").split(":", 1)[-1]
    cm = re.sub(r"\(R\)|\(TM\)|\bCPU\b|@.*$|\d+(st|nd|rd|th) Gen ", "", cm)
    h["cpu_model"] = " ".join(cm.split())
    # GPUs: name (lspci), usage (Intel: rc6 idle residency), power state and VRAM (largest PCI BAR)
    names_by_pci = {}
    for line in (d.get("cmd:gpu") or "").splitlines():
        addr, _, desc = line.partition(" ")
        desc = desc.split(": ", 1)[-1]
        vendor = desc.split()[0] if desc else ""
        m = re.search(r"\[([^\]]+)\]", desc)
        name = m.group(1) if m else re.sub(r"\(rev.*\)", "", desc).strip()
        if vendor and not name.lower().startswith(vendor.lower()):
            name = f"{vendor} {name}"
        names_by_pci[addr] = name
    gpus = []
    tnow = time.time()
    for line in (d.get("cmd:gpudrv") or "").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        card, pci, drv = parts
        base = f"/sys/class/drm/{card}"
        g = {"name": names_by_pci.get(pci.split(":", 1)[-1], drv), "driver": drv, "pct": None,
             "state": (d.get(f"{base}/device/power/runtime_status") or "").strip() or None, "vram": None}
        rc6 = (d.get(f"{base}/gt/gt0/rc6_residency_ms") or "").strip()
        if rc6.isdigit():
            prev = _prev_gpu.get(card)
            if prev:
                dt = (tnow - prev[1]) * 1000
                g["pct"] = round(max(0.0, min(100.0, 100 * (1 - (int(rc6) - prev[0]) / dt))), 1) if dt > 0 else 0.0
            _prev_gpu[card] = (int(rc6), tnow)
        # Integrated (Intel on bus 00): uses system RAM, no dedicated VRAM
        if drv in ("i915", "xe") and pci.split(":")[-2] == "00":
            g["vram"] = "shared"
        else:
            sizes = []
            for l in (d.get(f"{base}/device/resource") or "").splitlines():
                f = l.split()
                if len(f) == 3 and f[0] != "0x0000000000000000" and int(f[2], 16) & 0x2000:   # prefetchable
                    sizes.append(int(f[1], 16) - int(f[0], 16) + 1)
            if sizes and max(sizes) >= 2 ** 29:
                gb = max(sizes) / 2 ** 30
                g["vram"] = f"{gb:.0f} GB" if gb >= 1 else f"{max(sizes) // 2 ** 20} MB"
        gpus.append(g)
    gpus.sort(key=lambda x: x["pct"] is None)   # GPUs with measurable usage first
    h["gpus"] = gpus
    # stable global IPv6 addresses (privacy "temporary" and "deprecated" ones change on their own)
    h["ipv6"] = [m.group(1) for line in (d.get("cmd:ip6") or "").splitlines()
                 if "temporary" not in line and "deprecated" not in line
                 for m in [re.search(r"inet6 ([0-9a-f:]+)/", line)] if m]
    top = (d.get("cmd:top") or "").split()
    if len(top) >= 2:
        h["top"] = (top[0], float(top[-1]))
    with LOCK:
        STATE["host"].update(h)


# ── Collector: Docker ───────────────────────────────────────────────────────
_prev_ctr = {}
_known_ctr = {}


def collect_docker():
    cfg = names().get("services", {})
    ctrs = docker_get("/containers/json?all=1")

    def stats(c):
        if c["State"] != "running":
            return c, None
        try:
            return c, docker_get(f"/containers/{c['Id']}/stats?stream=false&one-shot=true")
        except Exception as e:
            log_err("stats " + c["Names"][0], e)
            return c, None

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(stats, ctrs))

    now = time.time()
    services, seen = [], set()
    for c, s in results:
        cname = c["Names"][0].lstrip("/")
        labels = c.get("Labels") or {}
        if labels.get("homelab.hide") == "true":
            continue
        seen.add(cname)
        over = cfg.get(cname, {})
        name = labels.get("homelab.name") or over.get("name") or pretty(cname)
        port = labels.get("homelab.port") or over.get("port")
        if not port:
            pubs = sorted({p["PublicPort"] for p in c.get("Ports", []) if p.get("PublicPort")})
            port = pubs[0] if pubs else None
        img = c.get("Image", "").split("/")[-1]
        if ":" not in img and not img.startswith("sha256"):
            img += ":latest"
        url = labels.get("homelab.url") or over.get("url")
        if not url and port:
            host = CONFIG["public_host"] or f'{STATE["host"].get("hostname") or "localhost"}.local'
            url = f"http://{host}:{port}" if int(port) != 80 else f"http://{host}"
        svc = {"id": cname, "name": name, "state": c["State"], "uptime": short_uptime(c.get("Status")), "url": url,
               "image": img, "port": int(port) if port else None, "cpu_pct": 0.0, "mem_mb": 0,
               "rx_kbps": 0, "tx_kbps": 0, "ms": LAT.get(cname)}
        if s:
            cpu = s.get("cpu_stats", {})
            total, system = cpu.get("cpu_usage", {}).get("total_usage", 0), cpu.get("system_cpu_usage", 0)
            ncpu = cpu.get("online_cpus") or 1
            nets = s.get("networks") or {}
            rx = sum(n.get("rx_bytes", 0) for n in nets.values())
            tx = sum(n.get("tx_bytes", 0) for n in nets.values())
            p = _prev_ctr.get(cname)
            if p and system > p["system"]:
                svc["cpu_pct"] = round((total - p["total"]) / (system - p["system"]) * ncpu * 100, 1)
                dt = now - p["t"]
                if nets:
                    svc["rx_kbps"] = round(max(0, rx - p["rx"]) / dt / 1000, 1)
                    svc["tx_kbps"] = round(max(0, tx - p["tx"]) / dt / 1000, 1)
            _prev_ctr[cname] = {"total": total, "system": system, "rx": rx, "tx": tx, "t": now}
            mem = s.get("memory_stats", {})
            usage = mem.get("usage", 0) - (mem.get("stats", {}) or {}).get("inactive_file", 0)
            svc["mem_mb"] = round(usage / 1048576, 1)
        h = SVC_HIST.setdefault(cname, deque([0.0] * HIST, maxlen=HIST))
        h.append(svc["cpu_pct"] if svc["state"] == "running" else 0.0)
        svc["hist"] = list(h)
        services.append(svc)
        # events on state changes
        old = _known_ctr.get(cname)
        if old is None and _known_ctr.get("__primed"):
            event("info", f"new container: {cname}")
        elif old and old != c["State"]:
            event("ok" if c["State"] == "running" else "warn", f"{cname} {c['State']}")
        _known_ctr[cname] = c["State"]
    for gone in [k for k in _known_ctr if not k.startswith("__") and k not in seen]:
        event("warn", f"container removed: {gone}")
        _known_ctr.pop(gone, None)
        SVC_HIST.pop(gone, None)
    _known_ctr["__primed"] = True
    services.sort(key=lambda x: (x["state"] != "running", x["name"].lower()))
    with LOCK:
        STATE["services"] = services


# ── Collector: service HTTP latency ───────────────────────────────────
def collect_latency():
    host = CONFIG["host_ip"]
    with LOCK:
        targets = [(s["id"], s["port"]) for s in STATE["services"] if s["port"] and s["state"] == "running"]
    for cname, port in targets:
        t0 = time.perf_counter()
        try:
            c = http.client.HTTPConnection(host, port, timeout=3)
            c.request("HEAD", "/")
            c.getresponse().read()
            c.close()
            LAT[cname] = max(1, round((time.perf_counter() - t0) * 1000))
        except Exception:
            LAT[cname] = None


# ── Collector: DNS (AdGuard Home) ────────────────────────────────────────────────
def dns_query(server, name, timeout=2):
    """Minimal A query over UDP. Returns the first IP or None."""
    qid = random.randint(0, 65535)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\0" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET6 if ":" in server else socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(q, (server, 53))
        data = s.recv(1024)
    finally:
        s.close()
    an = struct.unpack(">H", data[6:8])[0]
    i = 12
    while data[i]:
        i += data[i] + 1
    i += 5
    for _ in range(an):
        i += 2 if data[i] & 0xC0 == 0xC0 else data.index(0, i) - i + 1
        typ, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
        i += 10
        if typ == 1 and rdlen == 4:
            return socket.inet_ntoa(data[i:i + 4])
        i += rdlen
    return None


def collect_dns():
    server = CONFIG["dns_server"] or CONFIG["host_ip"]
    out = {"resolve_ok": False, "block_ok": False, "queries": None, "blocked": None,
           "avg_ms": None, "clients": None, "hist": []}
    try:
        out["resolve_ok"] = bool(dns_query(server, "google.com"))
    except Exception:
        pass
    try:
        out["block_ok"] = dns_query(server, "ad.doubleclick.net") in ("0.0.0.0", None)
    except Exception:
        pass
    want = CONFIG["dns_ipv6"]
    with LOCK:
        have = STATE["host"].get("ipv6")      # None until the host was read once
    if want and have is not None:
        v6 = {"expected": want, "present": want in have, "answers": False,
              "current": next((a for a in have if not a.startswith("fd")), None)}
        if v6["present"]:
            try:
                dns_query(want, "google.com")
                v6["answers"] = True
            except Exception:
                pass
        v6["ok"] = v6["present"] and v6["answers"]
        out["ipv6"] = v6
    if CONFIG["adguard_user"]:
        try:
            req = urllib.request.Request(CONFIG["adguard_url"].rstrip("/") + "/control/stats")
            tok = b64encode(f'{CONFIG["adguard_user"]}:{CONFIG["adguard_pass"]}'.encode()).decode()
            req.add_header("Authorization", "Basic " + tok)
            with urllib.request.urlopen(req, timeout=5) as r:
                st = json.load(r)
            out.update(queries=st.get("num_dns_queries"), blocked=st.get("num_blocked_filtering"),
                       avg_ms=round((st.get("avg_processing_time") or 0) * 1000, 1),
                       clients=len(st.get("top_clients") or []), hist=st.get("dns_queries") or [])
        except Exception as e:
            log_err("adguard", e)
    with LOCK:
        prev = STATE["dns"]
        if prev and (prev["resolve_ok"] and prev["block_ok"]) and not (out["resolve_ok"] and out["block_ok"]):
            event("err", "DNS degraded")
        elif prev and not (prev["resolve_ok"] and prev["block_ok"]) and out["resolve_ok"] and out["block_ok"]:
            event("ok", "DNS recovered")
        was, now = (prev or {}).get("ipv6"), out.get("ipv6")
        if now and (was is None or was["ok"]) and not now["ok"]:
            if not now["present"] and now["current"]:
                event("err", f'IPv6 changed: set router DHCPv6 DNS to {now["current"]}')
            elif not now["present"]:
                event("err", "IPv6 DNS address is gone (no global IPv6)")
            else:
                event("err", "IPv6 DNS not answering")
        elif now and was and not was["ok"] and now["ok"]:
            event("ok", "IPv6 DNS ok")
        STATE["dns"] = out


# ── Collector: network devices ───────────────────────────────────────
def ping(ip):
    try:
        r = subprocess.run(["ping", "-n", "-c", "1", "-W", "1", ip], capture_output=True, text=True, timeout=3)
        m = re.search(r"time=([\d.]+)", r.stdout)
        return round(float(m.group(1)), 1) if r.returncode == 0 and m else None
    except Exception:
        return None


def neighbors():
    try:
        r = subprocess.run(["ip", "neigh"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return {}
    return {m.group(1): m.group(2) for m in re.finditer(r"^(\d+\.\d+\.\d+\.\d+) .*lladdr (\S+)", r, re.M)}


def valid_name(n):
    """Reject 'names' that are really IPs or numbers (e.g. '10')."""
    return bool(n) and not re.fullmatch(r"[\d.]+", n) and n != "_gateway"


def _read_name(data, i):
    out = []
    while True:
        n = data[i]
        if n == 0:
            return ".".join(out), i + 1
        if n & 0xC0 == 0xC0:
            ptr = struct.unpack(">H", data[i:i + 2])[0] & 0x3FFF
            return ".".join(out + [_read_name(data, ptr)[0]]), i + 2
        out.append(data[i + 1:i + 1 + n].decode(errors="replace"))
        i += n + 1


def mdns_name(ip, timeout=1.5):
    """Name the device announces over mDNS (reverse PTR query to 224.0.0.251)."""
    rev = ".".join(reversed(ip.split("."))) + ".in-addr.arpa"
    q = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)
    q += b"".join(bytes([len(p)]) + p.encode() for p in rev.split(".")) + b"\0" + struct.pack(">HH", 12, 0x8001)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        for dst in (("224.0.0.251", 5353), (ip, 5353)):
            try:
                s.sendto(q, dst)
                data = s.recv(2048)
            except OSError:
                continue
            qd, an = struct.unpack(">HH", data[4:8])
            i = 12
            for _ in range(qd):
                i = _read_name(data, i)[1] + 4
            for _ in range(an):
                _, i = _read_name(data, i)
                typ, _, _, rdlen = struct.unpack(">HHIH", data[i:i + 10])
                i += 10
                if typ == 12:
                    name = _read_name(data, i)[0]
                    return name[:-6] if name.endswith(".local") else name
                i += rdlen
    except Exception:
        return None
    finally:
        s.close()
    return None


def rdns(ip):
    """Announced name: mDNS first, then reverse DNS. None if it announces nothing."""
    n = mdns_name(ip)
    if valid_name(n):
        return n
    try:
        name = socket.gethostbyaddr(ip)[0]
    except Exception:
        return None
    try:
        ipaddress.ip_address(name)
        return None          # got the IP itself back
    except ValueError:
        pass
    short = name.split(".")[0]
    return short if valid_name(short) else None


def save_devices():
    save_json(CONFIG["state_file"], {"devices": DEV})


def scan_network():
    net = ipaddress.ip_network(CONFIG["lan_cidr"], strict=False)
    hosts = [str(h) for h in net.hosts()]
    with ThreadPoolExecutor(64) as ex:
        res = dict(zip(hosts, ex.map(ping, hosts)))
    alive = {ip: ms for ip, ms in res.items() if ms is not None}
    macs = neighbors()
    now = time.time()
    for ip, ms in alive.items():
        if ip not in DEV:
            DEV[ip] = {"ip": ip, "first_seen": now, "name": rdns(ip), "mac": macs.get(ip)}
            if not ip == CONFIG["host_ip"]:
                event("info", f"new device: {DEV[ip]['name'] or UNKNOWN} ({ip})")
        DEV[ip].update(last_seen=now)
        if macs.get(ip):
            DEV[ip]["mac"] = macs[ip]
    # forget devices not seen for a long time
    pinned = set(names().get("devices", {}))
    for ip in [ip for ip, d in DEV.items()
               if ip not in pinned and now - d.get("last_seen", now) > CONFIG["forget_after"]]:
        event("info", f"forgot device: {DEV[ip].get('name') or ip}")
        DEV.pop(ip)
    save_devices()
    event("info", f"network scan: {len(alive)} devices")


def discover_passive(cfg):
    """Add devices pinned in names.json and the ones showing up in the ARP table."""
    now = time.time()
    for ip in cfg:
        DEV.setdefault(ip, {"ip": ip, "first_seen": now, "name": None, "mac": None})
    try:
        net = ipaddress.ip_network(CONFIG["lan_cidr"], strict=False)
    except ValueError:
        return
    for ip, mac in neighbors().items():
        if ipaddress.ip_address(ip) not in net or ip == CONFIG["host_ip"]:
            continue
        if ip not in DEV:
            DEV[ip] = {"ip": ip, "first_seen": now, "name": rdns(ip), "mac": mac, "last_seen": now}
            event("info", f"new device: {DEV[ip]['name'] or UNKNOWN} ({ip}) [arp]")
        else:
            DEV[ip]["mac"] = mac


def collect_devices():
    cfg = names().get("devices", {})
    gw = STATE["host"].get("gateway")
    discover_passive(cfg)
    ips = list(DEV)
    with ThreadPoolExecutor(32) as ex:
        res = dict(zip(ips, ex.map(ping, ips)))
    now = time.time()
    out = []
    for ip in ips:
        d, ms = DEV[ip], res[ip]
        was = d.get("state")
        d["state"] = "online" if ms is not None else "offline"
        if ms is not None:
            d["last_seen"] = now

        h = d.setdefault("hist", [])
        h.append(ms or 0)
        del h[:-HIST]
        # retry the announced name hourly while it has none
        if not d.get("name") and now - d.get("name_tried", 0) > 3600:
            d["name"], d["name_tried"] = rdns(ip), now
        over = cfg.get(ip, {})
        shown = over.get("name") or d.get("name") or UNKNOWN
        if was and was != d["state"]:
            event("ok" if ms is not None else "warn", f"{shown} ({ip}) {d['state']}")
        kind = over.get("kind") or ("router" if ip == gw else "server" if ip == CONFIG["host_ip"] else "device")
        out.append({"id": ip, "ip": ip, "name": shown, "kind": kind,
                    "state": d["state"], "ms": round(ms) if ms is not None else None, "hist": list(h)})
    out.sort(key=lambda x: ipaddress.ip_address(x["ip"]))
    with LOCK:
        STATE["devices"] = out


# ── Collector: weather (Open-Meteo, no API key) ────────────────────────────────────────────────────────
WMO = [(0, "CLEAR"), (1, "MOSTLY CLEAR"), (2, "PARTLY CLOUDY"), (3, "OVERCAST"), (48, "FOG"),
       (57, "DRIZZLE"), (67, "RAIN"), (77, "SNOW"), (82, "SHOWERS"), (86, "SNOW"), (99, "STORM")]


def collect_weather():
    if not (CONFIG["lat"] and CONFIG["lon"]):
        return
    q = urllib.parse.urlencode({"latitude": CONFIG["lat"], "longitude": CONFIG["lon"],
                                "current": "temperature_2m,weather_code"})
    with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=10) as r:
        cur = json.load(r)["current"]
    code = cur.get("weather_code", 0)
    cond = next((label for limit, label in WMO if code <= limit), "—")
    with LOCK:
        STATE["weather"] = {"temp_c": cur.get("temperature_2m"), "cond": cond, "place": CONFIG["place"]}


# ── Scheduled tasks (LG webOS Developer Mode countdown) ────────────────────────────────────────
_lgtv_seen = {"checked_at": None, "status": None}


def collect_tasks():
    d = load_json(CONFIG["lgtv_file"], None)
    tasks = []
    if d:
        rem = d.get("remaining_s")
        if rem is not None:
            rem = max(0, rem - (time.time() - d.get("checked_at", time.time())))
        tasks.append({"id": "lgtv", "name": "LG DevMode", "status": d.get("status"),
                      "remaining_s": rem, "checked_at": d.get("checked_at"), "msg": d.get("msg", ""),
                      "full_s": 1000 * 3600})
        if d.get("checked_at") != _lgtv_seen["checked_at"]:
            if _lgtv_seen["checked_at"] is not None or d.get("status") == "fail":
                lvl = "ok" if d.get("status") == "ok" else "err"
                event(lvl, f"LG DevMode: {d.get('msg', '')}")
            _lgtv_seen.update(checked_at=d.get("checked_at"), status=d.get("status"))
    with LOCK:
        STATE["tasks"] = tasks


# ── Periodic summary for the event log ──────────────────────────────────────────
def summary():
    with LOCK:
        s, h = STATE["services"], STATE["host"]
    up = sum(1 for x in s if x["state"] == "running")
    event("ok" if up == len(s) else "warn", f"{up}/{len(s)} services nominal")
    if h.get("top"):
        event("info", f"top: {h['top'][0]} {h['top'][1]:.0f}% cpu")


# ── status.json ────────────────────────────────────────────────────────────
def status():
    with LOCK:
        h = STATE["host"]
        gb = 1024 ** 3
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "site": {"brand": CONFIG["brand"], "name": CONFIG["name"], "role": CONFIG["role"]},
            "weather": STATE["weather"],
            "host": {
                "name": h.get("hostname"), "ip": CONFIG["host_ip"], "os": h.get("os"), "kernel": h.get("kernel"),
                "uptime_s": h.get("uptime_s"), "load": h.get("load", []), "procs": h.get("procs"),
                "temp_c": h.get("temp_c"), "swap_pct": h.get("swap_pct", 0),
                "cpu": {"pct": h.get("cpu_pct", 0.0), "threads": h.get("threads"), "model": h.get("cpu_model")},
                "gpus": h.get("gpus", []),
                "mem": {"used_gb": round(h.get("mem_used", 0) / gb, 1), "total_gb": round(h.get("mem_total", 0) / gb)},
                "disk": {"used_gb": round(h.get("disk_used", 0) / gb, 1), "total_gb": round(h.get("disk_total", 0) / gb),
                         "free_gb": round((h.get("disk_total", 0) - h.get("disk_used", 0)) / gb, 1)},
                "battery": {"pct": h.get("battery_pct"), "ac": h.get("ac", False)} if "battery_pct" in h else None,
                "net": {"rx_kbps": h.get("rx_kbps", 0), "tx_kbps": h.get("tx_kbps", 0)},
            },
            "services": STATE["services"],
            "tasks": STATE["tasks"],
            "devices": STATE["devices"],
            "dns": STATE["dns"],
            "events": list(EVENTS)[-12:],
        }


# ── Web server ───────────────────────────────────────────────────────────
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=os.path.join(HERE, "web"), **k)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self):
        if self.path.split("?")[0] == "/status.json":
            body = json.dumps(status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *a):
        pass


# ── Startup ───────────────────────────────────────────────────────────────
def every(seconds, fn, name, delay=0):
    def loop():
        time.sleep(delay)
        while True:
            t0 = time.time()
            try:
                fn()
            except Exception as e:
                log_err(name, e)
            time.sleep(max(0.5, seconds - (time.time() - t0)))
    threading.Thread(target=loop, name=name, daemon=True).start()


def detect_network():
    """Fill in HOST_IP, gateway and LAN_CIDR from the host default route."""
    d = host_dump()
    m = re.search(r"via (\S+) dev \S+(?: .*src (\S+))?", d.get("cmd:route") or "")
    if m:
        STATE["host"]["gateway"] = m.group(1)
        if not CONFIG["host_ip"] and m.group(2):
            CONFIG["host_ip"] = m.group(2)
    if not CONFIG["lan_cidr"] and CONFIG["host_ip"]:
        CONFIG["lan_cidr"] = str(ipaddress.ip_network(CONFIG["host_ip"] + "/24", strict=False))


def main():
    detect_network()
    DEV.update(load_json(CONFIG["state_file"], {}).get("devices", {}))
    for d in DEV.values():               # drop invalid names saved earlier
        if not valid_name(d.get("name")):
            d["name"] = None
    print(f"modo: {'ssh ' + CONFIG['ssh_target'] if CONFIG['ssh_target'] else 'local'} · "
          f"host {CONFIG['host_ip']} · LAN {CONFIG['lan_cidr']} · puerto {CONFIG['port']}", flush=True)
    event("ok", "dashboard collector started")
    every(CONFIG["t_fast"], collect_host, "host")
    every(CONFIG["t_fast"], collect_docker, "docker")
    every(CONFIG["t_latency"], collect_latency, "latency", delay=2)
    every(CONFIG["t_latency"], collect_dns, "dns", delay=1)
    every(CONFIG["t_scan"], scan_network, "scan")
    every(CONFIG["t_ping"], collect_devices, "devices", delay=8)
    every(CONFIG["t_weather"], collect_weather, "weather")
    every(30, collect_tasks, "tasks")
    every(CONFIG["summary_every"], summary, "summary", delay=15)
    every(60, save_devices, "save", delay=60)
    socketserver.TCPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer((CONFIG["bind"], CONFIG["port"]), Handler)
    print(f"dashboard en http://{CONFIG['bind']}:{CONFIG['port']}/", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
