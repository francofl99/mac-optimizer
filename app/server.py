#!/usr/bin/env python3
"""mac-optimizer dashboard. Usage: server.py [--port N] | --metrics"""
import base64
import collections
import configparser
import ipaddress
import json
import os
import plistlib
import re
import secrets
import signal
import socket
import threading
import shutil
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import schedule

BASE = Path(os.environ.get("MAC_OPTIMIZER_HOME", Path.home() / ".mac-optimizer"))
REPO = Path(__file__).resolve().parent.parent
REPORTS = BASE / "reports"
STATE = BASE / "state.json"
DASHBOARD = Path(__file__).with_name("dashboard.html")
EXEC_LOG = BASE / "exec.log"
GB = 1024 ** 3


def sh(cmd, timeout=15, merge=False):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
                              text=True, timeout=timeout).stdout
    except Exception:
        return ""


def memory():
    total = int(sh(["sysctl", "-n", "hw.memsize"]) or 0)
    page = int(sh(["pagesize"]) or 16384)
    vm = {}
    for line in sh(["vm_stat"]).splitlines():
        m = re.match(r'"?(.+?)"?:\s+(\d+)', line)
        if m:
            vm[m.group(1)] = int(m.group(2)) * page
    swap = re.findall(r"(\w+) = ([\d.]+)M", sh(["sysctl", "-n", "vm.swapusage"]))
    swap = {k: float(v) * 1024 ** 2 for k, v in swap}
    free_pct = re.search(r"(\d+)%", sh(["memory_pressure", "-Q"]))
    return {
        "total_gb": total / GB,
        "wired_gb": vm.get("Pages wired down", 0) / GB,
        "active_gb": vm.get("Pages active", 0) / GB,
        "inactive_gb": vm.get("Pages inactive", 0) / GB,
        "compressed_gb": vm.get("Pages occupied by compressor", 0) / GB,
        "free_gb": vm.get("Pages free", 0) / GB,
        "free_pct": int(free_pct.group(1)) if free_pct else None,
        "swap_used_gb": swap.get("used", 0) / GB,
        "swap_total_gb": swap.get("total", 0) / GB,
    }


def top_apps(limit=12):
    apps = {}
    for line in sh(["ps", "-Axo", "rss=,pcpu=,comm="]).splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        rss, cpu, comm = int(parts[0]), float(parts[1]), parts[2]
        m = re.search(r"/([^/]+)\.app/", comm)
        name = m.group(1) if m else comm.rsplit("/", 1)[-1]
        a = apps.setdefault(name, {"name": name, "mb": 0, "cpu": 0.0, "procs": 0})
        a["mb"] += rss / 1024
        a["cpu"] += cpu
        a["procs"] += 1
    rows = list(apps.values())
    return {
        "by_ram": sorted(rows, key=lambda r: -r["mb"])[:limit],
        "by_cpu": sorted(rows, key=lambda r: -r["cpu"])[:limit],
        "process_count": sum(r["procs"] for r in rows),
    }


_battery_cache = {"t": 0, "v": {}}


def battery_now():
    out = sh(["pmset", "-g", "batt"])
    pct = re.search(r"(\d+)%", out)
    return {
        "percent": int(pct.group(1)) if pct else None,
        "source": "AC" if "AC Power" in out else "Battery",
        "state": (re.search(r"%; ([^;]+);", out) or [None, None])[1],
    }


def battery():
    v = battery_now()
    if time.time() - _battery_cache["t"] > 600:
        prof = sh(["system_profiler", "SPPowerDataType"], timeout=30)
        cyc = re.search(r"Cycle Count: (\d+)", prof)
        cap = re.search(r"Maximum Capacity: (\d+)", prof)
        cond = re.search(r"Condition: (\w+)", prof)
        _battery_cache.update(t=time.time(), v={
            "cycles": int(cyc.group(1)) if cyc else None,
            "max_capacity": int(cap.group(1)) if cap else None,
            "condition": cond.group(1) if cond else None,
        })
    v.update(_battery_cache["v"])
    blockers = re.search(r"sleep prevented by ([^)]+)", sh(["pmset", "-g"]))
    v["sleep_blockers"] = sorted(set(s.strip() for s in blockers.group(1).split(","))) if blockers else []
    return v


ORBSTACK_IMG = Path.home() / "Library/Group Containers/HUAQ24HBR6.dev.orbstack/data/data.img.raw"


def docker():
    # `docker system df` takes minutes with hundreds of volumes; live view only uses cheap calls.
    names = sh(["docker", "ps", "--format", "{{.Names}}"], timeout=5).split()
    vm_disk = ORBSTACK_IMG.stat().st_blocks * 512 / GB if ORBSTACK_IMG.exists() else None
    return {"running": len(names), "containers": names, "vm_disk_gb": vm_disk}


_machine = {}


def machine():
    if not _machine:
        hw = sh(["system_profiler", "SPHardwareDataType"], timeout=30)
        field = lambda k: (re.search(rf"{k}: (.+)", hw) or [None, None])[1]
        _machine.update(model=field("Model Name"), chip=field("Chip") or field("Processor Name"),
                        macos=sh(["sw_vers", "-productVersion"]).strip() or None)
    return _machine


def disk():
    du = shutil.disk_usage("/System/Volumes/Data")
    return {"total_gb": du.total / GB, "used_gb": du.used / GB, "free_gb": du.free / GB}


PING_HOST = "1.1.1.1"
VPN_IFACES = ("utun", "ipsec", "ppp")
NET_POOL = ThreadPoolExecutor(3)
_ports = {"t": 0, "v": []}
_wifi_cache = {"t": 0, "v": None}
_net = {"v": None}


def service_order():
    if time.time() - _ports["t"] > 300:
        out = sh(["networksetup", "-listnetworkserviceorder"])
        _ports.update(t=time.time(), v=re.findall(r"Hardware Port: ([^,]+), Device: (\w+)\)", out))
    return _ports["v"]


def uplink():
    # With a VPN the default route is a utun; the scoped route shows the physical port and router underneath.
    for port, dev in service_order():
        gw = re.search(r"gateway: (\S+)", sh(["route", "-n", "get", "-ifscope", dev, "default"], timeout=3))
        if gw:
            return {"port": port, "device": dev, "gateway": gw.group(1)}
    return {}


def ping(host, count=5):
    out = sh(["ping", "-q", "-c", str(count), "-i", "0.2", "-W", "1000", host], timeout=count + 3)
    loss = re.search(r"([\d.]+)% packet loss", out)
    rtt = re.search(r"= [\d.]+/([\d.]+)/[\d.]+/([\d.]+) ms", out)
    return {"host": host, "ms": round(float(rtt.group(1)), 1) if rtt else None,
            "jitter_ms": round(float(rtt.group(2)), 1) if rtt else None,
            "loss_pct": float(loss.group(1)) if loss else None}


def dns_ms():
    m = re.search(r"Query time: (\d+) msec", sh(["dig", "+tries=1", "+time=2", "apple.com"], timeout=4))
    return int(m.group(1)) if m else None


def iface_bytes(dev):
    for line in sh(["netstat", "-ibn", "-I", dev]).splitlines():
        if "<Link#" in line:
            cols = line.split()
            return int(cols[-5]), int(cols[-2])
    return None


def wifi():
    if time.time() - _wifi_cache["t"] > 60:
        cur = sh(["system_profiler", "SPAirPortDataType"], timeout=30).partition("Current Network Information:")[2]
        cur = cur.partition("Other Local Wi-Fi Networks:")[0]
        field = lambda k: (re.search(rf"{k}: (.+)", cur) or [None, None])[1]
        sn = re.search(r"Signal / Noise: (-?\d+) dBm / (-?\d+) dBm", cur)
        rate = field("Transmit Rate")
        _wifi_cache.update(t=time.time(), v={
            "rssi": int(sn.group(1)) if sn else None, "noise": int(sn.group(2)) if sn else None,
            "channel": field("Channel"), "phy": field("PHY Mode"),
            "tx_rate_mbps": int(rate) if rate and rate.isdigit() else None,
        } if cur else None)
    return _wifi_cache["v"]


TRUSTED_FILE = BASE / "trusted-networks.txt"
norm_mac = lambda m: ":".join(p.zfill(2) for p in m.lower().split(":"))


def gateway_mac(ip):
    m = re.search(r" at ([0-9a-f:]{11,17}) ", sh(["arp", "-n", ip], timeout=3))
    return norm_mac(m.group(1)) if m else None


def trusted_macs():
    try:
        lines = TRUSTED_FILE.read_text().splitlines()
    except OSError:
        return set()
    return {norm_mac(l.split("#")[0].strip()) for l in lines if l.split("#")[0].strip()}


def network():
    up = uplink()
    via = (re.search(r"interface: (\S+)", sh(["route", "-n", "get", "default"], timeout=3)) or [None, None])[1]
    gw = NET_POOL.submit(ping, up["gateway"]) if up else None
    inet, dns = NET_POOL.submit(ping, PING_HOST), NET_POOL.submit(dns_ms)
    gateway = gw.result() if gw else None
    # The router's MAC identifies the home network: the SSID is redacted without Location permission.
    mac = gateway_mac(up["gateway"]) if up else None
    return {
        "t": time.time(), "uplink": up, "route_via": via,
        "vpn": via if via and via.startswith(VPN_IFACES) else None,
        "gateway": gateway, "internet": inet.result(), "dns_ms": dns.result(),
        "gateway_mac": mac, "trusted": bool(mac) and mac in trusted_macs(),
        "wifi": wifi() if up.get("port") == "Wi-Fi" else None,
        "bytes": iface_bytes(up["device"]) if up else None,
    }


def throughput(prev, cur):
    if not (prev and prev["bytes"] and cur["bytes"]) or prev["uplink"] != cur["uplink"]:
        return None, None
    dt = cur["t"] - prev["t"]
    return tuple(round(max(0, c - p) * 8 / dt) for p, c in zip(prev["bytes"], cur["bytes"]))


SPEEDTEST = {"status": "idle", "result": None}
SPEEDTEST_LOCK = threading.Lock()
SPEEDTEST_FILE = BASE / "speedtest.json"


def run_speedtest():
    try:
        r = json.loads(sh(["networkQuality", "-c"], timeout=120))
        result = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "down_bps": r.get("dl_throughput"),
                  "up_bps": r.get("ul_throughput"), "rpm": r.get("responsiveness"),
                  "base_rtt_ms": r.get("base_rtt"), "interface": r.get("interface_name"),
                  "server": r.get("test_endpoint")}
        SPEEDTEST_FILE.write_text(json.dumps(result))
        SPEEDTEST.update(status="ok", result=result)
    except (ValueError, OSError):
        SPEEDTEST.update(status="error")


def start_speedtest():
    with SPEEDTEST_LOCK:
        if SPEEDTEST["status"] == "running":
            return 409, {"error": "El test ya está corriendo"}
        SPEEDTEST["status"] = "running"
    threading.Thread(target=run_speedtest, daemon=True).start()
    return 200, SPEEDTEST


def set_trusted(trusted):
    up = uplink()
    mac = gateway_mac(up["gateway"]) if up else None
    if not mac:
        return 409, {"error": "No se pudo identificar el router de esta red"}
    keep = [l for l in (TRUSTED_FILE.read_text().splitlines() if TRUSTED_FILE.exists() else [])
            if norm_mac(l.split("#")[0].strip() or "x") != mac]
    if trusted:
        keep.append(f"{mac}  # router {up['gateway']}, {time.strftime('%Y-%m-%d')}")
    TRUSTED_FILE.write_text("\n".join(keep) + "\n" if keep else "")
    if _net["v"] and _net["v"]["gateway_mac"] == mac:
        _net["v"]["trusted"] = trusted
    return 200, {"gateway_mac": mac, "trusted": trusted}


SERVICE_KINDS = [("_androidtvremote2", "Android TV"), ("_googlecast", "Chromecast"), ("_airplay", "AirPlay"),
                 ("_raop", "AirPlay"), ("_companion-link", "Apple"), ("_hap", "HomeKit"), ("_ipp", "Impresora"),
                 ("_printer", "Impresora"), ("_scanner", "Escáner"), ("_uscan", "Escáner"),
                 ("_spotify-connect", "Spotify Connect"), ("_smb", "Archivos compartidos"), ("_ssh", "SSH")]
MAX_SCAN_HOSTS = 1024
SCAN = {"status": "idle", "result": None}
SCAN_LOCK = threading.Lock()


def dns_unescape(s):
    raw = re.sub(rb"\\(\d{3})", lambda m: bytes([int(m.group(1))]), s.encode())
    return re.sub(rb"\\(.)", rb"\1", raw).decode(errors="replace").replace("\xa0", " ")


def mdns(ip, *query):
    # Unicast mDNS straight to the device; it answers "legacy" queries even though the port isn't 5353.
    out = sh(["dig", "+short", "+time=1", "+tries=1", "-p", "5353", f"@{ip}", *query], timeout=4)
    return [l for l in out.splitlines() if l and not l.startswith(";")]


def identify(ip):
    # Some devices answer with every record they own (A, SRV, TXT…), so keep only lines of the asked shape.
    host = next((l for l in mdns(ip, "-x", ip) if l.endswith(".local.")), None)
    services = mdns(ip, "_services._dns-sd._udp.local", "PTR")
    known = [(s, label) for t, label in SERVICE_KINDS for s in services if s.startswith(t + ".")]
    name = None
    if known:
        inst = next((l for l in mdns(ip, known[0][0], "PTR") if l.endswith("." + known[0][0])), None)
        name = re.sub(r"-[0-9a-f]{32}$", "", dns_unescape(inst.removesuffix("." + known[0][0]))) if inst else None
    if not name and host:
        name = dns_unescape(host).removesuffix(".local.")
    return name, list(dict.fromkeys(label for _, label in known))


def lan(dev):
    me = sh(["ipconfig", "getifaddr", dev]).strip()
    return me, ipaddress.ip_interface(f"{me}/{sh(['ipconfig', 'getoption', dev, 'subnet_mask']).strip()}").network


def run_scan(up, mac, me, net):
    dev = up["device"]
    with ThreadPoolExecutor(64) as pool:
        list(pool.map(lambda h: sh(["ping", "-q", "-c", "1", "-W", "500", "-b", dev, str(h)], timeout=3),
                      [h for h in net.hosts() if str(h) != me]))
        found = {}
        for ip, hw in re.findall(r"\(([\d.]+)\) at ([0-9a-f:]+) on " + dev, sh(["arp", "-an", "-i", dev])):
            if ipaddress.ip_address(ip) in net and ip != me:
                found[ip] = norm_mac(hw)
        names = dict(zip(found, pool.map(identify, found)))
    devices = [{"ip": me, "mac": None, "name": sh(["scutil", "--get", "ComputerName"]).strip() or None,
                "kinds": [], "role": "self", "private": False}]
    for ip, hw in found.items():
        name, kinds = names[ip]
        devices.append({"ip": ip, "mac": hw, "name": name, "kinds": kinds,
                        "role": "router" if ip == up["gateway"] else None,
                        "private": bool(int(hw[:2], 16) & 2)})
    devices.sort(key=lambda d: ({"router": 0, "self": 1}.get(d["role"], 2), ipaddress.ip_address(d["ip"])))
    SCAN.update(status="ok", result={"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "gateway_mac": mac,
                                     "subnet": str(net), "devices": devices})


def start_scan():
    up = uplink()
    mac = gateway_mac(up["gateway"]) if up else None
    if not mac or mac not in trusted_macs():
        return 403, {"error": "Esta red no está marcada como de casa: no se escanea"}
    try:
        me, net = lan(up["device"])
    except ValueError:
        return 409, {"error": "Sin dirección IPv4 en esta red"}
    if net.num_addresses > MAX_SCAN_HOSTS:
        return 400, {"error": "La subred es demasiado grande para escanearla"}
    with SCAN_LOCK:
        if SCAN["status"] == "running":
            return 409, {"error": "La búsqueda ya está corriendo"}
        SCAN["status"] = "running"

    def job():
        try:
            run_scan(up, mac, me, net)
        except (ValueError, OSError):
            SCAN.update(status="error")

    threading.Thread(target=job, daemon=True).start()
    return 200, scan_state()


def scan_state():
    # Results stay in memory and only for the network they came from: device names can identify people.
    res = SCAN["result"]
    cur = (_net["v"] or {}).get("gateway_mac")
    return {**SCAN, "result": res if res and res["gateway_mac"] == cur else None}


FIREWALL = "/usr/libexec/ApplicationFirewall/socketfilterfw"
XPROTECT_RULES = Path("/Library/Apple/System/Library/CoreServices/XProtect.bundle/Contents/Resources/XProtect.yara")
SHARING_PORTS = {22: "SSH", 445: "archivos (SMB)", 548: "archivos (AFP)", 3283: "gestión remota", 5900: "pantalla"}
LOOPBACK = ("127.", "::1", "fe80::1%lo0", "localhost")
LAUNCH_DIRS = [(Path.home() / "Library/LaunchAgents", "Tu usuario"), (Path("/Library/LaunchAgents"), "Todos los usuarios"),
               (Path("/Library/LaunchDaemons"), "Sistema (root)")]
INTERPRETERS = {"sh", "bash", "zsh", "python", "python3", "node", "osascript", "perl", "ruby", "env"}
BASELINE_FILE = BASE / "launch-baseline.json"
NEW_FOR = 7 * 86400
SECURITY_EVERY = 600
UPDATES_EVERY = 6 * 3600
SECURITY = {"status": "idle", "result": None, "t": 0}
SECURITY_LOCK = threading.Lock()
_updates = {"t": 0, "v": None}


def check(cid, label, lvl, value, fix=None):
    return {"id": cid, "label": label, "level": lvl, "value": value, "fix": fix if lvl != "ok" else None}


def pref(domain, key):
    return sh(["defaults", "read", domain, key]).strip() or None


def listeners():
    found = set()
    for line in sh(["netstat", "-anv", "-p", "tcp"]).splitlines():
        # Columns after the state: rxbytes txbytes rhiwat shiwat process:pid. Names may contain spaces.
        m = re.match(r"tcp\S*\s+\d+\s+\d+\s+(\S+)\.(\d+)\s+\S+\s+LISTEN\s+(?:\d+\s+){4}(.+?):(\d+)\s", line)
        if m:
            found.add((m.group(1), int(m.group(2)), m.group(3), int(m.group(4))))
    return found


FW_CMD = "sudo /usr/libexec/ApplicationFirewall/socketfilterfw"
FW_INTERPRETERS = {"python", "python3", "ruby", "node", "perl", "php", "java", "bun", "deno"}
# Apple services that show up as firewall rules: what they are, the port that shows they are on, and a note.
FW_KNOWN = {
    "sshd-keygen-wrapper": ("Inicio de sesión remoto (SSH)", 22, None),
    "smbd": ("Compartir archivos (SMB)", 445, None),
    "cupsd": ("Compartir impresoras", 631, None),
    "screensharingd": ("Compartir pantalla", 5900, None),
    "sharingd": ("AirDrop y Handoff", None, "Necesaria para AirDrop."),
    "remoted": ("Depuración de iPhone o iPad por red (Xcode)", None, "Solo hace falta si depuras en un dispositivo por Wi-Fi."),
    "remotepairingdeviced": ("Emparejamiento de iPhone o iPad (Xcode)", None, "Solo hace falta si depuras en un dispositivo por Wi-Fi."),
}


def exe_path(pid):
    # Some processes rename themselves (node shows "next-server"), so ps alone can lose the path.
    comm = sh(["ps", "-o", "comm=", "-p", str(pid)]).strip()
    if comm.startswith("/"):
        return comm
    txt = re.search(r"^n(/.+)$", sh(["lsof", "-nP", "-a", "-p", str(pid), "-d", "txt", "-Fn"]), re.M)
    return txt.group(1) if txt else None


def fw_rules():
    out = sh([FIREWALL, "--listapps"])
    return [(path.strip(), "block" not in rule.lower()) for path, rule in re.findall(r"^\d+ : (.+?)\s*\n\s*\((.+?)\)", out, re.M)]


def fw_app(path, allowed, ports, sign):
    base = re.sub(r"\.(app|systemextension)$", "", Path(path).name)
    app = {"path": path, "name": base, "allowed": allowed, "what": None, "fix": None}
    if not allowed:
        return {**app, "level": "ok", "advice": "Bloqueada."}
    remove, block = f"`{FW_CMD} --remove {path}`", f"`{FW_CMD} --blockapp {path}`"
    if base in FW_INTERPRETERS:
        return {**app, "what": f"Intérprete de {base}", "level": "warn", "fix": block,
                "advice": f"Cualquier script de {base} puede recibir conexiones de la red. Bloquéala salvo que sirvas algo a otros "
                          "equipos: lo que escucha solo en localhost no la necesita."}
    if base in FW_KNOWN:
        what, port, note = FW_KNOWN[base]
        if port and port not in ports:
            return {**app, "what": what, "level": "warn", "fix": remove, "advice": "El servicio está apagado: la regla sobra."}
        return {**app, "what": what, "level": "info" if note and note.startswith("Solo") else "ok", "fix": remove if note else None,
                "advice": note or "La usa un servicio que está activo."}
    if "tailscale" in path.lower():
        return {**app, "what": "Tailscale", "level": "ok", "advice": "Necesaria para recibir conexiones por Tailscale."}
    if not Path(path).exists():
        return {**app, "level": "warn", "fix": remove, "advice": "El programa ya no existe: la regla sobra."}
    sig = sign(path)
    if not sig["signed"] or not sig["valid"]:
        return {**app, "level": "warn", "fix": block, "advice": "Sin firma válida: confirma que la reconoces antes de dejarla pasar."}
    return {**app, "level": "info", "advice": f"Firmada por {sig['signer'] or 'su desarrollador'}. Déjala solo si otros equipos se conectan a ella."}


def fw_option(oid, label, on, recommended, why, arg, level=None):
    lvl = level or ("ok" if on == recommended else "warn")
    return {"id": oid, "label": label, "on": on, "recommended": recommended, "level": lvl, "why": why,
            "fix": f"Ajustes › Red › Firewall › Opciones, o en tu terminal: `{FW_CMD} {arg}`" if lvl != "ok" else None}


def firewall(open_ports, ports):
    enabled = "enabled" in sh([FIREWALL, "--getglobalstate"])
    stealth = sh([FIREWALL, "--getstealthmode"]).strip().endswith(" on")
    block_all = "enabled" in sh([FIREWALL, "--getblockall"])
    signed_out = sh([FIREWALL, "--getallowsigned"])
    builtin = bool(re.search(r"built-in signed software ENABLED", signed_out))
    downloaded = bool(re.search(r"downloaded signed software ENABLED", signed_out))
    net = _net["v"] or {}
    # With no network marked as home there is nothing to compare against, so block-all gets no advice.
    trusted = net.get("trusted") if net.get("gateway_mac") and trusted_macs() else None
    sigs = {}
    sign = lambda path: sigs.setdefault(path, signature(path))

    options = [
        fw_option("stealth", "Modo invisible", stealth, True,
                  "El Mac no responde a pings ni a sondeos de puertos: cuesta más encontrarlo en una red ajena.",
                  "--setstealthmode on"),
        fw_option("allow_signed", "Permitir software descargado firmado", downloaded, False,
                  "Activado, cualquier app descargada con firma válida recibe conexiones sin preguntarte. "
                  "Apagado, macOS pregunta la primera vez y la regla queda en la lista.", "--setallowsignedapp off"),
        fw_option("allow_builtin", "Permitir software integrado", builtin, True,
                  "AirDrop, AirPlay y los servicios de compartir de Apple funcionan sin reglas a mano.", "--setallowsigned on"),
        fw_option("block_all", "Bloquear todas las conexiones entrantes", block_all, trusted is False,
                  ("Estás en una red que no marcaste como de casa: actívalo mientras sigas en ella. " if trusted is False else
                   "En tu red de casa no hace falta. " if trusted else "") +
                  "Corta todo lo entrante salvo DHCP e IPsec, incluidos AirDrop y compartir.",
                  "--setblockall on" if trusted is False else "--setblockall off",
                  level="info" if block_all and trusted is not False else None),
    ]
    rules = fw_rules()
    apps = sorted((fw_app(path, ok, ports, sign) for path, ok in rules), key=lambda a: (LEVELS[a["level"]], a["name"]))

    def reach(path):
        if not enabled:
            return True, "sin firewall"
        if block_all:
            return False, "bloquear todo"
        rule = next((ok for p, ok in rules if path == p or path.startswith(p.rstrip("/") + "/")), None)
        if rule is not None:
            return rule, "regla: permitir" if rule else "regla: bloquear"
        sig = sign(path) if path else {"signed": False}
        if sig.get("signer") == "Apple" and builtin:
            return True, "software de Apple"
        if sig.get("signed") and sig.get("valid") and not sig.get("adhoc") and downloaded:
            return True, "firma de desarrollador"
        return False, "sin regla: bloqueado hasta que lo permitas"

    exposed = {}
    for _, port, proc, pid in open_ports:
        e = exposed.setdefault(proc, {"process": proc, "ports": set(), "pids": set()})
        e["ports"].add(port)
        e["pids"].add(pid)
    for e in exposed.values():
        e["path"] = exe_path(min(e.pop("pids")))
        e["reachable"], e["why"] = reach(e["path"])
        e["ports"] = sorted(e["ports"])
    exposed = sorted(exposed.values(), key=lambda e: (not e["reachable"], -len(e["ports"])))
    return {"enabled": enabled, "stealth": stealth, "trusted_network": trusted, "options": options, "apps": apps}, exposed


def pending_updates():
    if time.time() - _updates["t"] > UPDATES_EVERY:
        out = sh(["softwareupdate", "--list"], timeout=120, merge=True)
        titles = re.findall(r"Title: (.+?), Version: ([^,]+)", out)
        _updates.update(t=time.time(), v=[f"{t} {v}" for t, v in titles] if "Finding available" in out else None)
    return _updates["v"]


def xprotect_info():
    m = re.search(r"Version: (\S+) Installed: (\S+ \S+)", sh(["xprotect", "version"]))
    if m:
        installed = time.mktime(time.strptime(m.group(2), "%Y-%m-%d %H:%M:%S"))
        return m.group(1), installed, "disabled" not in sh(["xprotect", "status"])
    try:
        return None, XPROTECT_RULES.stat().st_mtime, True
    except OSError:
        return None, None, False


def posture(open_ports, all_ports):
    checks = []
    fv = sh(["fdesetup", "status"])
    checks.append(check("filevault", "FileVault", "ok" if "is On" in fv else "warn" if "progress" in fv else "bad",
                        "disco cifrado" if "is On" in fv else "cifrando…" if "progress" in fv else "disco sin cifrar",
                        "Actívalo en Ajustes › Privacidad y seguridad › FileVault."))
    sip = "enabled" in sh(["csrutil", "status"])
    checks.append(check("sip", "Protección del sistema (SIP)", "ok" if sip else "bad", "activa" if sip else "desactivada",
                        "Reactívala desde el modo de recuperación con `csrutil enable`."))
    gk = "enabled" in sh(["spctl", "--status"])
    checks.append(check("gatekeeper", "Gatekeeper", "ok" if gk else "bad",
                        "solo apps firmadas" if gk else "acepta apps sin verificar", "En tu terminal: `sudo spctl --global-enable`."))

    fw, exposed = firewall(open_ports, {port for _, port, _, _ in all_ports})
    bad_opts = [o for o in fw["options"] if o["level"] == "warn"]
    bad_apps = [a for a in fw["apps"] if a["level"] == "warn"]
    todo = [("Activa" if o["recommended"] else "Desactiva") + f" «{o['label']}»" for o in bad_opts]
    if bad_apps:
        todo.append("bloquea o quita las reglas de " + ", ".join(a["name"] for a in bad_apps))
    issues = len(bad_opts) + len(bad_apps)
    checks.append(check("firewall", "Firewall", ("warn" if issues else "ok") if fw["enabled"] else "bad" if open_ports else "warn",
                        (f"activo · {issues} {'punto' if issues == 1 else 'puntos'} a revisar" if issues else "activo") if fw["enabled"] else "apagado",
                        "; ".join(todo).capitalize() + "." if fw["enabled"] else
                        "Actívalo en Ajustes › Red › Firewall; con servicios abiertos a la red es lo primero."))

    version, installed, scans = xprotect_info()
    age = (time.time() - installed) / 86400 if installed else None
    checks.append(check("xprotect", "XProtect", "bad" if age is None or not scans else level_of(age, 30, 90),
                        "no se encontró" if age is None else
                        f"{'v' + version + ' · ' if version else ''}actualizado hace {round(age)} d{'' if scans else ' · escaneos apagados'}",
                        "Revisa que las actualizaciones de seguridad automáticas estén activas."))

    su = "/Library/Preferences/com.apple.SoftwareUpdate"
    security_auto = pref(su, "CriticalUpdateInstall") != "0" and pref(su, "ConfigDataInstall") != "0"
    macos_auto = pref(su, "AutomaticallyInstallMacOSUpdates") != "0"
    store_auto = pref("/Library/Preferences/com.apple.commerce", "AutoUpdate") != "0"
    manual = [n for n, on in (("seguridad", security_auto), ("macOS", macos_auto), ("App Store", store_auto)) if not on]
    checks.append(check("auto_updates", "Actualizaciones automáticas",
                        "bad" if not security_auto else "warn" if manual else "ok",
                        "todas activas" if not manual else f"manuales: {', '.join(manual)}",
                        "Ajustes › General › Actualización de software › Actualizaciones automáticas: activa todas."))

    pending = pending_updates()
    if pending is not None:
        checks.append(check("pending_updates", "Actualizaciones pendientes", "warn" if pending else "ok",
                            ", ".join(pending) if pending else "ninguna", "Instálalas desde Ajustes › General › Actualización de software."))

    lock = sh(["sysadminctl", "-screenLock", "status"], merge=True)
    delay = re.search(r"delay is (\d+) seconds", lock)
    secs = 0 if "immediate" in lock else int(delay.group(1)) if delay else None
    checks.append(check("screen_lock", "Bloqueo de pantalla", "bad" if secs is None else level_of(secs, 6, 3600),
                        "no pide contraseña" if secs is None else "inmediato" if secs == 0 else f"pide contraseña a los {fmt_secs(secs)}",
                        "Ajustes › Pantalla bloqueada › Pedir contraseña: inmediatamente."))

    lw = "/Library/Preferences/com.apple.loginwindow"
    auto_login, guest = pref(lw, "autoLoginUser"), pref(lw, "GuestEnabled") == "1"
    checks.append(check("login", "Inicio de sesión", "bad" if auto_login else "warn" if guest else "ok",
                        f"automático como {auto_login}" if auto_login else "cuenta de invitado activa" if guest else "con contraseña",
                        "Ajustes › Usuarios y grupos: desactiva el inicio automático y la cuenta de invitado."))

    sharing = sorted({SHARING_PORTS[p] for _, p, _, _ in open_ports if p in SHARING_PORTS})
    checks.append(backups())

    checks.append(check("sharing", "Compartir", "warn" if sharing else "ok", ", ".join(sharing) if sharing else "nada activo",
                        "Ajustes › General › Compartir: apaga lo que no uses."))

    reachable = [e for e in exposed if e["reachable"]]
    n_ports = len({p for _, p, _, _ in open_ports})
    checks.append(check("exposed", "Abierto a la red", "ok" if not reachable else "warn" if fw["enabled"] else "bad",
                        f"{n_ports} puertos de {len(exposed)} procesos · {len(reachable)} alcanzables" if exposed else "nada, solo localhost",
                        "Publica los puertos de Docker como 127.0.0.1:puerto:puerto y cierra lo que no uses."))
    return checks, exposed, fw


LEVELS = {"bad": 0, "warn": 1, "info": 2, "ok": 3}


def level_of(v, warn, bad):
    return "bad" if v >= bad else "warn" if v >= warn else "ok"


def fmt_secs(s):
    return f"{s // 60} min" if s >= 60 else f"{s} s"


def signature(path):
    out = sh(["codesign", "-dv", "--verbose=2", path], merge=True)
    if "not signed" in out:
        return {"signed": False}
    if "Signature=adhoc" in out:
        return {"signed": True, "valid": True, "signer": None, "adhoc": True}
    auth = re.search(r"Authority=(.+)", out)
    valid = subprocess.run(["codesign", "--verify", path], capture_output=True, timeout=60).returncode == 0
    who = auth.group(1) if auth else None
    who = "Apple" if who in ("Software Signing", "macOS Software Signing") else re.sub(r"^Developer ID Application: (.+?)(?: \(\w+\))?$", r"\1", who or "") or None
    return {"signed": True, "valid": valid, "signer": who}


def launch_items():
    try:
        baseline = json.loads(BASELINE_FILE.read_text())
    except (OSError, ValueError):
        baseline = None
    # The first run records what is already there, so only items added later count as new.
    first, baseline, now = baseline is None, baseline or {}, time.time()
    items = []
    for folder, scope in LAUNCH_DIRS:
        for plist in sorted(folder.glob("*.plist")):
            try:
                pl = plistlib.loads(plist.read_bytes())
            except (OSError, ValueError, plistlib.InvalidFileException):
                pl = None
            args = (pl or {}).get("ProgramArguments") or []
            prog = (pl or {}).get("Program") or (args[0] if args else None)
            seen = baseline.get(str(plist))
            if not seen or seen.get("program") != prog:
                seen = baseline[str(plist)] = {"first_seen": 0 if first else now, "program": prog}
            item = {"path": str(plist), "label": (pl or {}).get("Label") or plist.stem, "scope": scope, "program": prog,
                    "new": now - seen["first_seen"] < NEW_FOR, "first_seen": seen["first_seen"] or None}
            if pl is None:
                item.update(level="warn", note="no se pudo leer el plist")
            elif not prog:
                item.update(level="info", note="vacío: no ejecuta nada")
            elif Path(prog).name in INTERPRETERS:
                script = next((a for a in args[1:] if not a.startswith("-")), "")
                item.update(level="warn" if item["new"] else "info", note=f"script: {script[:120]}")
            elif not Path(prog).exists():
                item.update(level="info", note="huérfano: el programa ya no existe, el plist se puede borrar")
            else:
                sig = signature(prog)
                item.update(sig)
                item["level"] = ("warn" if not sig["signed"] else "bad" if not sig["valid"] else "warn" if item["new"]
                                 else "info" if sig.get("adhoc") else "ok")
                item["note"] = ("sin firmar" if not sig["signed"] else "firma inválida" if not sig["valid"]
                                else "firma ad-hoc, sin identidad" if sig.get("adhoc") else sig["signer"] or "firmado")
            items.append(item)
    BASELINE_FILE.write_text(json.dumps(baseline, indent=1))
    return items


HOME = Path.home()
SSH_DIR = HOME / ".ssh"
# Package managers install unsigned or ad-hoc binaries by design; what runs from here is not suspicious on its own.
PKG_DIRS = (str(HOME / ".nvm") + "/", "/opt/homebrew/", "/usr/local/Cellar/", str(HOME / ".pyenv") + "/", str(HOME / ".cargo") + "/")
PNPM_CONF = HOME / "Library/Preferences/pnpm"
EOL_FALLBACK = {
    "nodejs": {"16": "2023-09-11", "18": "2025-04-30", "19": "2023-06-01", "20": "2026-04-30", "21": "2024-06-01",
               "22": "2027-04-30", "23": "2025-06-01", "24": "2028-04-30", "25": "2026-06-01", "26": "2029-04-30"},
    "python": {"3.8": "2024-10-07", "3.9": "2025-10-31", "3.10": "2026-10-31", "3.11": "2027-10-31", "3.12": "2028-10-31",
               "3.13": "2029-10-31", "3.14": "2030-10-31"},
}
_eol = {}


def home_tilde(p):
    return str(p).replace(str(HOME), "~", 1)


def read_home(rel):
    try:
        return (HOME / rel).read_text(errors="replace")
    except OSError:
        return ""


def user_bin(name):
    return shutil.which(name, path=RUN_ENV.get("PATH"))


def first_upper(s):
    return s[:1].upper() + s[1:]


def backups():
    if "No destinations" in sh(["tmutil", "destinationinfo"], merge=True):
        return check("backups", "Copias de seguridad", "warn", "Time Machine sin configurar",
                     "Conecta un disco y actívalo en Ajustes › General › Time Machine, con la opción de cifrar.")
    # Reading the latest backup needs Full Disk Access; without it all that is known is that one is configured.
    m = re.search(r"(\d{4}-\d{2}-\d{2})-(\d{6})", sh(["tmutil", "latestbackup"], merge=True))
    if not m:
        return check("backups", "Copias de seguridad", "info", "Time Machine configurado")
    age = (time.time() - time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))) / 86400
    return check("backups", "Copias de seguridad", level_of(age, 7, 30), f"última hace {round(age)} d",
                 "Conecta el disco de Time Machine para que haga una copia.")


def key_encrypted(text):
    if "OPENSSH PRIVATE KEY" not in text:
        return "ENCRYPTED" in text
    try:
        raw = base64.b64decode("".join(l for l in text.splitlines() if not l.startswith("-----")))
    except ValueError:
        return None
    if not raw.startswith(b"openssh-key-v1\0"):
        return None
    n = int.from_bytes(raw[15:19], "big")
    return raw[19:19 + n] != b"none"


def git_root(path):
    for p in path.parents:
        if p in (HOME, Path("/")):
            return None
        if (p / ".git").exists():
            return p
    return None


def ssh_keys():
    paths = {f for f in [*SSH_DIR.glob("*"), *SSH_DIR.glob("*/*")] if f.is_file() and f.suffix != ".pub"}
    for m in re.finditer(r'^\s*IdentityFile\s+"?([^"\n]+?)"?\s*$', read_home(".ssh/config"), re.M | re.I):
        if "%" not in m.group(1):
            paths.add(Path(os.path.expanduser(m.group(1))))
    keys = []
    for p in sorted(paths):
        try:
            if p.stat().st_size > 16384:
                continue
            text, mode = p.read_text(errors="replace"), p.stat().st_mode & 0o777
        except OSError:
            continue
        if "PRIVATE KEY-----" in text[:100]:
            keys.append({"name": p.name, "encrypted": key_encrypted(text), "open": bool(mode & 0o077), "repo": git_root(p)})
    return keys


def ssh_check():
    keys = ssh_keys()
    plain = [k["name"] for k in keys if k["encrypted"] is False]
    in_repo = [k for k in keys if k["repo"]]
    loose = [k["name"] for k in keys if k["open"]]
    todo = []
    if plain:
        todo.append(f"sin passphrase: {', '.join(plain)}. Ponles una con `ssh-keygen -p -f <clave>`")
    if in_repo:
        todo.append("dentro de un repo git: " + ", ".join(f"{k['name']} ({home_tilde(k['repo'])})" for k in in_repo)
                    + ". Muévelas a `~/.ssh`")
    if loose:
        todo.append(f"permisos abiertos: {', '.join(loose)}. Ciérralos con `chmod 600 <clave>`")
    lvl = "bad" if any(k["repo"] and k["encrypted"] is False for k in keys) else "warn" if todo else "ok"
    value = (f"{len(keys)} claves · " + (f"{len(plain)} sin passphrase" if plain else "todas con passphrase")) if keys else "ninguna"
    return check("ssh", "Claves SSH", lvl, value, first_upper("; ".join(todo)) + "." if todo else None)


def credentials_check():
    found = []
    aws = configparser.ConfigParser()
    try:
        aws.read_string(read_home(".aws/credentials"))
    except configparser.Error:
        pass
    static = [s for s in aws.sections() if aws.has_option(s, "aws_access_key_id") and not aws.has_option(s, "aws_session_token")]
    if static:
        found.append(("AWS", f"claves fijas en ~/.aws/credentials ({', '.join(static)}): usa `aws configure sso` o `credential_process`"))
    for rel in (".npmrc", "Library/Preferences/pnpm/rc", "Library/Preferences/pnpm/auth.ini", "Library/Preferences/pnpm/config.yaml"):
        if re.search(r"(_authToken|_auth|_password)\s*[=:]\s*(?!\$\{)\S", read_home(rel)):
            found.append(("npm", f"token en ~/{rel}: cámbialo por `${{NPM_TOKEN}}` y carga la variable desde tu gestor"))
    if re.search(r"\bpassword\s+\S", read_home(".netrc")):
        found.append(("netrc", "contraseñas en ~/.netrc: bórralas y usa el llavero"))
    try:
        docker = json.loads(read_home(".docker/config.json") or "{}")
    except ValueError:
        docker = {}
    if not docker.get("credsStore") and any(isinstance(a, dict) and a.get("auth") for a in docker.get("auths", {}).values()):
        found.append(("Docker", 'logins en ~/.docker/config.json: agrega `"credsStore": "osxkeychain"` y vuelve a hacer `docker login`'))
    if re.search(r"oauth_token:\s*\S", read_home(".config/gh/hosts.yml")):
        found.append(("GitHub CLI", "token en ~/.config/gh/hosts.yml: `gh auth logout` y `gh auth login` lo guardan en el llavero"))
    if read_home(".git-credentials").strip():
        found.append(("git", "contraseñas en ~/.git-credentials: `git config --global credential.helper osxkeychain` y borra el archivo"))
    if re.search(r"^\s*password\s*[=:]\s*\S", read_home(".pypirc"), re.M):
        found.append(("PyPI", "contraseña en ~/.pypirc: usa un token guardado con `keyring`"))
    return check("credentials", "Credenciales en texto plano", "warn" if found else "ok",
                 ", ".join(dict.fromkeys(n for n, _ in found)) if found else "ninguna a la vista",
                 first_upper("; ".join(f for _, f in found)) + "." if found else None)


def conf_keys(text):
    return {k.replace("-", "").lower(): v.strip("'\"") for k, v in re.findall(r"^\s*([\w.@/:-]+?)\s*[=:]\s*(.*?)\s*$", text, re.M)}


def package_managers_check():
    npm = conf_keys(read_home(".npmrc"))
    pnpm = {**conf_keys(read_home(PNPM_CONF / "rc")), **conf_keys(read_home(PNPM_CONF / "config.yaml"))}
    parts, todo, lvl = [], [], "ok"
    if user_bin("npm"):
        age = npm.get("minreleaseage", "")
        if age.isdigit() and int(age) > 0:
            parts.append(f"npm espera {age} d")
        else:
            parts.append("npm sin espera")
            todo.append("npm instala versiones recién publicadas: `npm config set min-release-age 3`")
    if user_bin("pnpm"):
        age, trust = pnpm.get("minimumreleaseage", ""), pnpm.get("trustpolicy")
        # pnpm's built-in one-day wait is not strict: a too-new version is excluded from the check instead of blocked.
        parts.append(f"pnpm espera {int(age) // 1440} d" if age.isdigit() and int(age) >= 1440 else "pnpm con la espera por defecto")
        if not (age.isdigit() and int(age) >= 1440):
            todo.append("fija la espera de pnpm para que sea estricta: `pnpm config set -g minimumReleaseAge 4320`")
        if trust != "no-downgrade":
            todo.append("frena versiones publicadas con menos garantías que la anterior: `pnpm config set -g trustPolicy no-downgrade`")
        if pnpm.get("dangerouslyallowallbuilds") == "true":
            lvl = "bad"
            todo.append("`dangerouslyAllowAllBuilds` deja correr los scripts de cualquier dependencia: quítalo")
    if not parts:
        return check("package_managers", "npm y pnpm", "ok", "no instalados")
    if todo:
        lvl = "warn" if lvl == "ok" else lvl
    elif npm.get("ignorescripts") != "true":
        lvl = "info"
        todo.append("opcional: `npm config set ignore-scripts true` evita que las dependencias corran scripts al instalar "
                    "(pnpm ya los bloquea por defecto)")
    return check("package_managers", "npm y pnpm", lvl, " · ".join(parts), first_upper("; ".join(todo)) + "." if todo else None)


def eol_dates(product):
    cache = _eol.setdefault(product, {"t": 0, "v": EOL_FALLBACK[product]})
    if time.time() - cache["t"] > 86400:
        cache["t"] = time.time()
        try:
            with urllib.request.urlopen(f"https://endoflife.date/api/{product}.json", timeout=5) as r:
                cache["v"] = {x["cycle"]: x["eol"] for x in json.load(r) if isinstance(x.get("eol"), str)}
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return cache["v"]


def runtime_installs():
    found = []
    for d in sorted((HOME / ".nvm/versions/node").glob("v*")):
        found.append(("node", d.name.split(".")[0][1:], d.name[1:], "nvm", f"`nvm uninstall {d.name[1:]}`"))
    for d in sorted(Path("/opt/homebrew/Cellar").glob("node*/*")):
        found.append(("node", d.name.split(".")[0], d.name, "Homebrew", f"`brew uninstall {d.parent.name}`"))
    if Path("/usr/local/bin/node").exists() and not os.path.realpath("/usr/local/bin/node").startswith("/usr/local/Cellar/"):
        v = sh(["/usr/local/bin/node", "-v"], timeout=5).strip().lstrip("v")
        if v:
            found.append(("node", v.split(".")[0], v, "instalador de nodejs.org",
                          "`sudo rm -rf /usr/local/bin/{node,npm,npx,corepack} /usr/local/lib/node_modules /usr/local/include/node`"))
    for d in sorted(Path("/opt/homebrew/Cellar").glob("python@*/*")):
        found.append(("python", d.parent.name.split("@")[1], d.name, "Homebrew", f"`brew uninstall {d.parent.name}`"))
    for d in sorted((HOME / ".pyenv/versions").glob("*")):
        m = re.match(r"\d+\.\d+", d.name)
        if m:
            found.append(("python", m.group(0), d.name, "pyenv", f"`pyenv uninstall {d.name}`"))
    return found


def node_default():
    alias = read_home(".nvm/alias/default").strip().lstrip("v")
    if alias:
        versions = sorted((d.name[1:] for d in (HOME / ".nvm/versions/node").glob("v*")
                           if d.name[1:] == alias or d.name[1:].startswith(alias + ".")),
                          key=lambda v: [int(x) for x in re.findall(r"\d+", v)])
        if versions:
            return versions[-1]
    node = user_bin("node")
    return sh([node, "-v"], timeout=5).strip().lstrip("v") or None if node else None


def runtimes_check():
    today = time.strftime("%Y-%m-%d")
    eol = {"node": eol_dates("nodejs"), "python": eol_dates("python")}
    dead = [i for i in runtime_installs() if eol[i[0]].get(i[1], "9999") <= today]
    default = node_default()
    default_dead = default and eol["node"].get(default.split(".")[0], "9999") <= today
    todo = []
    if default_dead:
        todo.append("tu node por defecto ya no recibe parches: `nvm install --lts` y `nvm alias default lts/*`")
    if dead:
        todo.append("sin soporte: " + "; ".join(f"{p} {v} ({where}) {how}" for p, _, v, where, how in dead))
    value = (f"node {default}" if default else "node no encontrado") + (f" · {len(dead)} versiones sin soporte" if dead else " · todo con soporte")
    return check("runtimes", "Versiones de node y python", "bad" if default_dead else "warn" if dead else "ok", value,
                 first_upper(". ".join(todo)) + "." if todo else None)


def docker_ports_check():
    docker = user_bin("docker")
    out = sh([docker, "ps", "--format", "{{.Names}}\t{{.Ports}}"], timeout=8) if docker else ""
    if not out.strip():
        return None
    published = {}
    for line in out.splitlines():
        name, _, ports = line.partition("\t")
        lan = sorted({int(p) for p in re.findall(r"(?:0\.0\.0\.0|\[::\]|:::?):(\d+)->", ports)})
        if lan:
            published[name] = lan
    orb = user_bin("orb")
    orb_lan = sh([orb, "config", "get", "docker.expose_ports_to_lan"], timeout=5).strip() if orb else ""
    n = sum(len(p) for p in published.values())
    if not published:
        return check("docker_ports", "Puertos de Docker", "ok", "solo en localhost")
    if orb_lan == "false":
        return check("docker_ports", "Puertos de Docker", "ok", f"{n} puertos, OrbStack los deja en localhost")
    listed = ", ".join(f"{c} ({', '.join(map(str, p[:4]))}{'…' if len(p) > 4 else ''})" for c, p in list(published.items())[:6])
    more = f" y {len(published) - 6} más" if len(published) > 6 else ""
    how = ("Con OrbStack, en tu terminal: `orb config set docker.expose_ports_to_lan false`; todos quedan en localhost sin tocar los compose."
           if orb else "En Docker Desktop › Settings › Docker Engine agrega `\"ip\": \"127.0.0.1\"` y recrea los contenedores.")
    return check("docker_ports", "Puertos de Docker", "warn", f"{n} puertos de {len(published)} contenedores abiertos a la red",
                 f"{how} Uno por uno: publica `127.0.0.1:puerto:puerto` en el compose. Abiertos: {listed}{more}.")


def dev_checks():
    return [c for c in (ssh_check(), credentials_check(), package_managers_check(), runtimes_check(), docker_ports_check()) if c]


CONN_EVERY = 30
CONN_WINDOW = 24 * 3600
LEARN_FOR = 86400
CONNS = {}
CONN_LOCK = threading.Lock()
OUTBOUND_FILE = BASE / "outbound-baseline.json"
OUTBOUND = {"since": None, "seen": {}}
TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")
SUSPECT_DIRS = ("/tmp/", "/private/tmp/", "/private/var/tmp/", "/Users/Shared/", str(HOME / "Downloads") + "/")
DOWNLOADERS = {"curl", "wget", "nc", "ncat", "socat", "osascript", "base64"}
SIG_POOL, DNS_POOL = ThreadPoolExecutor(2), ThreadPoolExecutor(4)
_pids, _sigs, _ptr = {}, {}, {}


def established():
    for line in sh(["netstat", "-anv", "-p", "tcp"]).splitlines():
        m = re.match(r"tcp\S*\s+\d+\s+\d+\s+\S+\s+(\S+)\.(\d+)\s+ESTABLISHED\s+(?:\d+\s+){4}(.+?):(\d+)\s", line)
        if not m:
            continue
        try:
            ip = ipaddress.ip_address(m.group(1).split("%")[0])
        except ValueError:
            continue
        if not (ip.is_loopback or ip.is_link_local or ip.is_unspecified):
            yield str(ip), int(m.group(2)), m.group(3), int(m.group(4))


def owner(pid, proc):
    hit = _pids.get(pid)
    if not hit or hit[0] != proc:
        path = exe_path(pid) or proc
        # Group helpers under their app: "Brave Browser.app/…/Helper.app/…" counts as Brave Browser.
        app = re.match(r"(.+?\.(?:app|systemextension))/", path)
        hit = _pids[pid] = (proc, app.group(1) if app else path)
    return hit[1]


def cached_signature(path):
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {"signed": False, "missing": True}
    hit = _sigs.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    if hit is None or hit[1] is not None:
        _sigs[path] = (mtime, None)
        SIG_POOL.submit(lambda: _sigs.__setitem__(path, (mtime, signature(path))))
    return None


def resolve(ip):
    try:
        _ptr[ip] = socket.gethostbyaddr(ip)[0]
    except OSError:
        _ptr[ip] = ""


def host_name(ip):
    addr = ipaddress.ip_address(ip)
    if addr in TAILSCALE_NET:
        return f"Tailscale {ip}"
    if addr.is_private:
        return f"red local {ip}"
    if ip not in _ptr:
        _ptr[ip] = None
        DNS_POOL.submit(resolve, ip)
    return _ptr[ip] or ip


def conn_sampler():
    try:
        OUTBOUND.update(json.loads(OUTBOUND_FILE.read_text()))
    except (OSError, ValueError):
        OUTBOUND.update(since=time.time(), seen={})
    while True:
        now, dirty = time.time(), False
        with CONN_LOCK:
            for ip, port, proc, pid in established():
                path = owner(pid, proc)
                entry = CONNS.setdefault((path, ip, port), [now, now, 0])
                entry[1], entry[2] = now, entry[2] + 1
                if path not in OUTBOUND["seen"]:
                    OUTBOUND["seen"][path], dirty = now, True
            for key in [k for k, e in CONNS.items() if now - e[1] > CONN_WINDOW]:
                del CONNS[key]
        if dirty:
            OUTBOUND_FILE.write_text(json.dumps(OUTBOUND, indent=1))
        if len(_pids) > 5000:
            _pids.clear()
        time.sleep(CONN_EVERY)


def outbound():
    now = time.time()
    with CONN_LOCK:
        entries = list(CONNS.items())
    by = {}
    for (path, ip, port), (_, last, n) in entries:
        p = by.setdefault(path, {"dests": collections.Counter(), "last": 0})
        p["dests"][(ip, port)] += n
        p["last"] = max(p["last"], last)
    rows = []
    for path, p in by.items():
        sig = cached_signature(path) if path.startswith("/") else {"signed": False}
        seen = OUTBOUND["seen"].get(path, now)
        new = seen - (OUTBOUND["since"] or now) > LEARN_FOR and now - seen < NEW_FOR
        managed = path.startswith(PKG_DIRS)
        if sig is None:
            lvl, note = "info", "verificando firma…"
        elif not sig.get("signed") or sig.get("adhoc"):
            what = "firma ad-hoc" if sig.get("adhoc") else "sin firmar"
            lvl, note = ("info", f"{what}, instalado con un gestor de paquetes") if managed else ("warn", what)
        elif not sig.get("valid"):
            lvl, note = "bad", "firma inválida"
        else:
            lvl, note = "ok", sig.get("signer") or "firmado"
        if new and lvl in ("ok", "info"):
            lvl = "warn"
        hosts = list(dict.fromkeys(host_name(ip) + ("" if port in (80, 443) else f":{port}") for (ip, port), _ in p["dests"].most_common(8)))
        rows.append({"name": re.sub(r"\.(app|systemextension)$", "", Path(path).name), "path": path, "level": lvl, "note": note, "new": new,
                     "hosts": hosts[:5], "destinations": len(p["dests"]), "active": now - p["last"] < CONN_EVERY * 2,
                     "last": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(p["last"]))})
    rows.sort(key=lambda r: (LEVELS[r["level"]], not r["active"], -r["destinations"]))
    return {"since": OUTBOUND["since"], "learning": now - (OUTBOUND["since"] or now) < LEARN_FOR, "every": CONN_EVERY,
            "window_h": CONN_WINDOW // 3600, "processes": rows}


def suspicious():
    procs = {}
    for line in sh(["ps", "-axww", "-o", "pid=,ppid=,comm="]).splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.+)$", line)
        if m:
            procs[int(m.group(1))] = (int(m.group(2)), m.group(3).strip())
    with CONN_LOCK:
        online = {path for (path, _, _), e in CONNS.items() if time.time() - e[1] < CONN_EVERY * 2}
    found = []
    for pid, (ppid, comm) in procs.items():
        name, parent = Path(comm).name, Path(procs.get(ppid, (0, ""))[1]).name
        why, lvl = None, "warn"
        if comm.startswith(SUSPECT_DIRS):
            why = "corre desde una carpeta temporal o de descargas"
        elif name in DOWNLOADERS and parent in FW_INTERPRETERS:
            why = f"lo lanzó {parent}: así suele bajar y correr código un paquete infectado"
        elif comm in online and not comm.startswith(PKG_DIRS):
            sig = cached_signature(comm)
            if sig and (not sig.get("signed") or sig.get("adhoc")):
                why = "sin firma y con conexiones a internet"
        elif comm.startswith("/") and not os.path.exists(comm):
            why, lvl = "su ejecutable ya no está en el disco (lo actualizaron o lo borraron)", "info"
        if why:
            found.append({"pid": pid, "name": name, "path": comm, "parent": parent, "level": lvl, "note": why})
    return sorted(found, key=lambda f: (LEVELS[f["level"]], f["name"]))


def refresh_security():
    try:
        all_ports = listeners()
        checks, exposed, fw = posture({l for l in all_ports if not l[0].startswith(LOOPBACK)}, all_ports)
        SECURITY.update(status="ok", t=time.time(), result={
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "checks": checks, "dev": dev_checks(), "firewall": fw, "exposed": exposed,
            "launch": launch_items(), "suspicious": suspicious()})
    except Exception:
        SECURITY.update(status="error", t=time.time())


def security_state():
    with SECURITY_LOCK:
        if SECURITY["status"] != "running" and time.time() - SECURITY["t"] > SECURITY_EVERY:
            SECURITY["status"] = "running"
            threading.Thread(target=refresh_security, daemon=True).start()
    return {k: SECURITY[k] for k in ("status", "result")}


SCAN_ROOTS = [Path.home() / "Downloads", Path.home() / "Desktop", Path("/Applications"), Path.home() / "Applications",
              Path("/Users/Shared"), Path("/private/tmp"), Path("/private/var/tmp")]
MALWARE = {"status": "idle", "result": None}
MALWARE_LOCK = threading.Lock()
MALWARE_FILE = BASE / "malware-scan.json"
MALWARE_EVERY = 24 * 3600
MALWARE_MAX_BYTES = 200 * 1024 ** 2


def yara_bin():
    return shutil.which("yara", path=RUN_ENV.get("PATH"))


def run_malware_scan(yara):
    start = time.time()
    launch = [i["program"] for i in (SECURITY["result"] or {}).get("launch", []) if i.get("program")]
    targets = [str(p) for p in SCAN_ROOTS if p.exists()] + [p for p in launch if Path(p).is_file()]
    targets_file = BASE / "malware-scan.targets"
    targets_file.write_text("\n".join(targets) + "\n")
    try:
        # XProtect's own rules: the same signatures macOS uses, so no third-party database to trust or keep current.
        p = subprocess.run(["nice", "-n", "15", yara, "-r", "-N", "-w", "-p", "2", f"--skip-larger={MALWARE_MAX_BYTES}",
                            "--scan-list", str(XPROTECT_RULES), str(targets_file)],
                           capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        MALWARE.update(status="error")
        return
    if p.returncode != 0 and not p.stdout and "error scanning" not in p.stderr:
        MALWARE.update(status="error")
        return
    matches = [{"rule": r, "path": f} for r, _, f in (l.partition(" ") for l in p.stdout.splitlines() if " " in l)]
    result = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "duration": round(time.time() - start),
              "roots": [t for t in targets if t not in launch], "launch_programs": len([t for t in targets if t in launch]),
              "matches": matches, "unreadable": p.stderr.count("error scanning"), "rules_version": xprotect_info()[0]}
    MALWARE_FILE.write_text(json.dumps(result, indent=1))
    MALWARE.update(status="ok", result=result)


def start_malware_scan():
    yara = yara_bin()
    if not yara:
        return 409, {"error": "Falta yara: instálalo con `brew install yara`"}
    if not XPROTECT_RULES.exists():
        return 409, {"error": "No se encontraron las reglas de XProtect"}
    with MALWARE_LOCK:
        if MALWARE["status"] == "running":
            return 409, {"error": "El escaneo ya está corriendo"}
        MALWARE["status"] = "running"
    threading.Thread(target=run_malware_scan, args=(yara,), daemon=True).start()
    return 200, malware_state()


def malware_state():
    return {**MALWARE, "yara": bool(yara_bin()), "every_h": MALWARE_EVERY // 3600}


def malware_scheduler():
    time.sleep(120)
    while True:
        last = (MALWARE["result"] or {}).get("ts")
        stale = not last or time.time() - time.mktime(time.strptime(last, "%Y-%m-%dT%H:%M:%S")) > MALWARE_EVERY
        # Several minutes of CPU: on battery it waits for the charger.
        if stale and battery_now()["source"] == "AC":
            start_malware_scan()
        time.sleep(3600)


def metrics():
    load = os.getloadavg()
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "cpus": os.cpu_count(),
        "machine": machine(),
        "load": [round(x, 2) for x in load],
        "memory": memory(),
        "disk": disk(),
        "battery": battery(),
        "apps": top_apps(),
        "docker": docker(),
        "network": _net["v"],
    }


SAMPLE_EVERY = 15
HISTORY = collections.deque(maxlen=24 * 3600 // SAMPLE_EVERY)
WINDOWS = {"15m": 900, "1h": 3600}
MAX_POINTS = 240


def sample():
    m = memory()
    net = network()
    net["rx_bps"], net["tx_bps"] = throughput(_net["v"], net)
    _net["v"] = net
    return {
        "t": int(time.time()),
        "mem": 100 - m["free_pct"] if m["free_pct"] is not None else None,
        "load": round(os.getloadavg()[0], 2),
        "swap": round(m["swap_used_gb"], 3),
        "disk": round(disk()["free_gb"], 2),
        "batt": battery_now()["percent"],
        "ping": net["internet"]["ms"],
        "gw": (net["gateway"] or {}).get("ms"),
        "rx": net["rx_bps"],
        "tx": net["tx_bps"],
    }


def sampler():
    while True:
        start = time.time()
        try:
            HISTORY.append(sample())
        except Exception:
            pass
        time.sleep(max(1, SAMPLE_EVERY - (time.time() - start)))


def boot_time():
    m = re.search(r"sec = (\d+)", sh(["sysctl", "-n", "kern.boottime"]))
    return int(m.group(1)) if m else 0


def history(window):
    since = boot_time() if window == "boot" else int(time.time()) - WINDOWS.get(window, 900)
    pts = [s for s in list(HISTORY) if s["t"] >= since]
    step = max(1, -(-len(pts) // MAX_POINTS))
    picked = pts[::-1][::step][::-1]
    return {"window": window, "since": since, "now": int(time.time()), "every": SAMPLE_EVERY, "samples": picked}


def load_state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def set_status(finding_id, status, note=""):
    state = load_state()
    state.setdefault("findings", {})[finding_id] = {
        "status": status, "note": note, "updated": time.strftime("%Y-%m-%dT%H:%M:%S")}
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    return state


def list_reports():
    out = []
    for p in sorted(REPORTS.glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text())
            out.append({"id": p.stem, "date": r.get("date"), "summary": r.get("summary"),
                        "metrics": r.get("metrics", {})})
        except ValueError:
            continue
    return out


def runnable(finding):
    cmd = (finding.get("command") or "").strip()
    if not cmd:
        return False, "Sin comando"
    if re.search(r"\bsudo\b", cmd):
        return False, "Requiere sudo: ejecútalo en tu terminal"
    return True, ""


JOBS, JOBS_LOCK, RUN_ENV = {}, threading.Lock(), {}
MAX_OUTPUT = 200_000
# stdin is /dev/null, so a CLI asking for confirmation reads EOF, aborts, and often still exits 0.
PROMPT_RE = re.compile(r"\[y/N\]|\(y/n\)|\[Y/n\]|Are you sure|Do you want to continue|Proceed\?", re.I)


def run_job(job):
    start = time.time()
    proc = subprocess.Popen(["/bin/bash", "-c", job["command"]], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                            cwd=str(Path.home()), env=RUN_ENV, start_new_session=True)
    job["pid"] = proc.pid
    for line in proc.stdout:
        if len(job["output"]) < MAX_OUTPUT:
            job["output"] += line
    proc.wait()
    prompted = bool(PROMPT_RE.search(job["output"][-2000:]))
    status = ("cancelled" if job.get("cancelled") else "needs_input" if prompted
              else "ok" if proc.returncode == 0 else "error")
    job.update(code=proc.returncode, duration=round(time.time() - start, 1), status=status)
    if job["status"] == "ok" and job["report"]:
        set_status(job["finding"], "done", f"ejecutado desde el dashboard ({job['duration']}s)")
    with EXEC_LOG.open("a") as f:
        f.write(json.dumps({k: job[k] for k in ("id", "report", "finding", "command", "status", "code", "duration", "started")},
                           ensure_ascii=False) + "\n")


def start_job(report_id, finding_id):
    path = REPORTS / f"{report_id}.json"
    if not re.fullmatch(r"[\w-]+", report_id or "") or not path.exists():
        return 404, {"error": "reporte no encontrado"}
    # The command always comes from the report on disk, never from the request body.
    finding = next((f for f in json.loads(path.read_text()).get("findings", []) if f.get("id") == finding_id), None)
    if not finding:
        return 404, {"error": "hallazgo no encontrado"}
    ok, why = runnable(finding)
    if not ok:
        return 400, {"error": why}
    return launch(report_id, finding_id, finding.get("title"), finding["command"])


def launch(report_id, finding_id, title, command):
    with JOBS_LOCK:
        if any(j["status"] == "running" and j["finding"] == finding_id for j in JOBS.values()):
            return 409, {"error": "Esta acción ya se está ejecutando"}
        job = {"id": secrets.token_hex(6), "report": report_id, "finding": finding_id, "title": title,
               "command": command, "status": "running", "output": "", "code": None,
               "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        JOBS[job["id"]] = job
    threading.Thread(target=run_job, args=(job,), daemon=True).start()
    return 200, job


class Handler(BaseHTTPRequestHandler):
    token = ""
    allowed_hosts = set()

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        # Blocks DNS-rebinding: a foreign site resolving to 127.0.0.1 still sends its own Host header.
        return self.headers.get("Host") in self.allowed_hosts

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, {"error": "host no permitido"})
        url = urlsplit(self.path)
        path = url.path
        if path in ("/", "/index.html"):
            html = DASHBOARD.read_text().replace("__CSRF_TOKEN__", self.token)
            return self.send(200, html.encode(), "text/html; charset=utf-8")
        if path == "/api/live":
            return self.send(200, metrics())
        if path == "/api/live/history":
            return self.send(200, history(parse_qs(url.query).get("window", ["15m"])[0]))
        if path == "/api/network/speedtest":
            return self.send(200, SPEEDTEST)
        if path == "/api/network/scan":
            return self.send(200, scan_state())
        if path == "/api/security":
            return self.send(200, security_state())
        if path == "/api/security/scan":
            return self.send(200, malware_state())
        if path == "/api/security/connections":
            return self.send(200, outbound())
        if path == "/api/reports":
            return self.send(200, list_reports())
        if path == "/api/state":
            return self.send(200, load_state())
        if path == "/api/schedule":
            return self.send(200, schedule.show())
        if path == "/api/jobs":
            jobs = sorted(JOBS.values(), key=lambda j: j["started"], reverse=True)[:20]
            return self.send(200, [{k: v for k, v in j.items() if k != "output"} for j in jobs])
        m = re.fullmatch(r"/api/jobs/(\w+)", path)
        if m and m.group(1) in JOBS:
            return self.send(200, JOBS[m.group(1)])
        m = re.fullmatch(r"/api/reports/([\w-]+)", path)
        if m and (REPORTS / f"{m.group(1)}.json").exists():
            return self.send(200, (REPORTS / f"{m.group(1)}.json").read_bytes())
        self.send(404, {"error": "not found"})

    def do_POST(self):
        # Token is only embedded in the served page, so other sites in the browser can't trigger commands.
        if not self.host_ok() or not secrets.compare_digest(self.headers.get("X-Token", ""), self.token):
            return self.send(403, {"error": "token inválido"})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        m = re.fullmatch(r"/api/findings/([\w.-]+)", self.path)
        if m:
            return self.send(200, set_status(m.group(1), body.get("status", "pending"), body.get("note", "")))
        if self.path == "/api/run":
            return self.send(*start_job(body.get("report"), body.get("finding")))
        if self.path == "/api/schedule":
            try:
                return self.send(200, schedule.apply(body.get("mode"), body.get("weekday"), body.get("hour", 10),
                                                     body.get("minute", 0), self.server.server_port))
            except (ValueError, TypeError, RuntimeError) as e:
                return self.send(400, {"error": str(e)})
        if self.path == "/api/network/speedtest":
            return self.send(*start_speedtest())
        if self.path == "/api/network/trust":
            return self.send(*set_trusted(bool(body.get("trusted"))))
        if self.path == "/api/network/scan":
            return self.send(*start_scan())
        if self.path == "/api/security/scan":
            return self.send(*start_malware_scan())
        if self.path == "/api/analyze":
            return self.send(*launch(None, "__analysis__", "Nuevo repaso (Claude headless)", f'bash "{REPO}/bin/analyze.sh"'))
        m = re.fullmatch(r"/api/jobs/(\w+)/cancel", self.path)
        if m and m.group(1) in JOBS and JOBS[m.group(1)]["status"] == "running":
            job = JOBS[m.group(1)]
            job["cancelled"] = True
            os.killpg(job["pid"], signal.SIGTERM)
            return self.send(200, job)
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    if "--metrics" in sys.argv:
        _net["v"] = network()
        print(json.dumps(metrics(), indent=2))
        sys.exit()
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8765
    REPORTS.mkdir(parents=True, exist_ok=True)
    try:
        SPEEDTEST.update(status="ok", result=json.loads(SPEEDTEST_FILE.read_text()))
    except (OSError, ValueError):
        pass
    try:
        MALWARE.update(status="ok", result=json.loads(MALWARE_FILE.read_text()))
    except (OSError, ValueError):
        pass
    RUN_ENV.update(os.environ, PATH=schedule.user_path(), HOMEBREW_NO_AUTO_UPDATE="1")
    Handler.token = secrets.token_urlsafe(24)
    Handler.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=malware_scheduler, daemon=True).start()
    threading.Thread(target=conn_sampler, daemon=True).start()
    print(f"Repaso dashboard: http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
