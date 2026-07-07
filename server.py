#!/usr/bin/env python3
"""
keenetic-mcp — an MCP server for managing Keenetic routers from your AI assistant.

Talks to the router's RCI JSON API over HTTP. Authentication uses Keenetic's
two-step MD5-challenge + SHA256 scheme; every call authenticates fresh (stateless).

Read tools (always available):
  list_devices, list_static_leases, find_free_ip, wan_status, list_port_forwards

Write tools (guarded; disabled unless KEENETIC_ENABLE_WRITES=1):
  set_static_lease, pin_device, rename_device, remove_static_lease, reboot

Configuration (environment variables):
  KEENETIC_URL           Router base URL          (default http://192.168.1.1)
  KEENETIC_USER          Admin username           (default admin)
  KEENETIC_PASS          Admin password           (required)
  KEENETIC_ENABLE_WRITES Set to 1/true/yes to enable write tools (default off)
  KEENETIC_HOST          Bind address for the MCP HTTP server (default 0.0.0.0)
  KEENETIC_PORT          Bind port                (default 8905)

Tested against Keenetic Hopper DSL (KN-3610). The RCI API is shared across the
Keenetic line (Giga, Viva, Hopper, …), so other models should work too.
"""
import os
import re
import json
import hashlib
import http.cookiejar
import urllib.request
import urllib.error

from mcp.server.fastmcp import FastMCP

BASE = os.environ.get("KEENETIC_URL", "http://192.168.1.1").rstrip("/")
USER = os.environ.get("KEENETIC_USER", "admin")
PASS = os.environ.get("KEENETIC_PASS", "")
HOST = os.environ.get("KEENETIC_HOST", "0.0.0.0")
PORT = int(os.environ.get("KEENETIC_PORT", "8905"))
ENABLE_WRITES = os.environ.get("KEENETIC_ENABLE_WRITES", "").strip().lower() in ("1", "true", "yes", "on")

mcp = FastMCP("keenetic", host=HOST, port=PORT)

_WRITE_DISABLED_MSG = (
    "🔒 Write tools are disabled. This tool modifies the router. "
    "To enable it, set KEENETIC_ENABLE_WRITES=1 in the server environment and restart the MCP server."
)


def _opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def _auth(op):
    """Keenetic MD5-challenge + SHA256 auth. On success `op` holds the session cookie."""
    try:
        op.open(BASE + "/auth", timeout=8)
        return True  # already authenticated (rare)
    except urllib.error.HTTPError as e:
        realm = e.headers.get("X-NDM-Realm")
        chal = e.headers.get("X-NDM-Challenge")
        if not (realm and chal):
            raise RuntimeError("Keenetic challenge headers were not returned (is KEENETIC_URL correct?)")
        md5 = hashlib.md5(f"{USER}:{realm}:{PASS}".encode()).hexdigest()
        sha = hashlib.sha256((chal + md5).encode()).hexdigest()
        body = json.dumps({"login": USER, "password": sha}).encode()
        req = urllib.request.Request(BASE + "/auth", data=body,
                                     headers={"Content-Type": "application/json"})
        r = op.open(req, timeout=8)
        return r.status == 200


def _rci(payload):
    """Authenticate + POST to the RCI endpoint. payload = list[dict] (RCI command tree)."""
    op = _opener()
    _auth(op)
    req = urllib.request.Request(BASE + "/rci/", data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(op.open(req, timeout=12).read().decode())


def _status_errors(obj):
    """Recursively collect entries with status=='error' (with their messages).
    Empty list = no error. Used to verify that write operations actually succeeded."""
    errs = []

    def walk(x):
        if isinstance(x, dict):
            if x.get("status") == "error":
                errs.append(str(x.get("message", "error")))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(obj)
    return errs


def _running_config_lines():
    out = _rci([{"show": {"running-config": {}}}])
    # running-config -> {"message": [lines]} on Keenetic Hopper. May also come back as a string.
    blob = out[0]["show"]["running-config"]
    if isinstance(blob, dict):
        blob = blob.get("message", blob)
    if isinstance(blob, list):
        return [str(ln).strip() for ln in blob]
    return [ln.strip() for ln in str(blob).replace("\\n", "\n").split("\n")]


def _active_leases():
    """Active DHCP lease list (always returns a list)."""
    out = _rci([{"show": {"ip": {"dhcp": {"bindings": {}}}}}])
    leases = out[0]["show"]["ip"]["dhcp"]["bindings"].get("lease", [])
    if isinstance(leases, dict):
        leases = [leases]
    return leases


def _static_lease_ips():
    """Set of IPs already used by static reservations (running-config 'ip dhcp host')."""
    ips = set()
    for ln in _running_config_lines():
        if ln.startswith("ip dhcp host"):
            parts = ln.split()  # ip dhcp host <mac> <ip>
            if len(parts) >= 5:
                ips.add(parts[4])
    return ips


def _lan_prefix(leases=None):
    """Derive the LAN /24 prefix + gateway IP from a reliable source.
    Prefer the router gateway (BASE = the router's own address, always present),
    then fall back to a lease/static IP."""
    host = BASE.split("//")[-1].split("/")[0].split(":")[0]
    if host.count(".") == 3 and all(p.isdigit() for p in host.split(".")):
        return host.rsplit(".", 1)[0], host  # (prefix, gateway_ip)
    cand = [str(l.get("ip", "")) for l in (leases or [])] + list(_static_lease_ips())
    for ip in cand:
        if ip.count(".") == 3 and all(p.isdigit() for p in ip.split(".")):
            return ip.rsplit(".", 1)[0], None
    return "192.168.1", None


def _match_devices(identifier):
    """Return devices in the active lease table matching `identifier` -> (leases, matches).
    IP/MAC = exact match; name = case-insensitive substring. An address-like identifier
    (only digits/dots/colons/hex with at least one digit) does NOT fall back to name
    substring — so an input like '66' won't accidentally match a device named 'Room-66-Cam'."""
    leases = _active_leases()
    q = identifier.strip().lower()
    if not q:
        return leases, []
    addressish = bool(re.fullmatch(r"[0-9a-f:.]+", q)) and any(c.isdigit() for c in q)
    exact, partial = [], []
    for l in leases:
        lip = str(l.get("ip", "")).lower()
        lmac = str(l.get("mac", "")).lower()
        lname = str(l.get("name") or l.get("hostname") or "").lower()
        if q == lip or q == lmac:
            exact.append(l)
        elif not addressish and q in lname:
            partial.append(l)
    return leases, (exact or partial)  # an exact IP/MAC match wins over a name match


def _resolve_device(identifier):
    """Resolve to a single device. Success: (dev, leases, None). Error: (None, leases, err_str)."""
    if not identifier.strip():
        return None, [], "❌ identifier cannot be empty (give an IP / MAC / name fragment)."
    leases, matches = _match_devices(identifier)
    if not matches:
        return None, leases, f"❌ '{identifier}' was not found in the active device list. Check with list_devices()."
    if len(matches) > 1:
        rows = "; ".join(
            f"{m.get('ip','?')}={(m.get('name') or m.get('hostname') or '?')}({m.get('mac','?')})"
            for m in matches)
        return None, leases, f"⚠️ '{identifier}' matched multiple devices: {rows}\nBe more specific (use IP or MAC)."
    return matches[0], leases, None


@mcp.tool()
def list_devices() -> str:
    """List active devices connected to the router (DHCP lease table): ip, mac, name, remaining lease time."""
    leases = _active_leases()
    lines = [f"# Active devices ({len(leases)})", "| IP | MAC | Name | Expires (s) |", "|---|---|---|---|"]
    for l in sorted(leases, key=lambda x: x.get("ip", "")):
        nm = (l.get("name") or l.get("hostname") or "?")
        lines.append(f"| {l.get('ip','?')} | {l.get('mac','?')} | {nm} | {l.get('expires','?')} |")
    return "\n".join(lines)


@mcp.tool()
def list_static_leases() -> str:
    """List static DHCP reservations — the 'ip dhcp host' lines in running-config."""
    rows = [ln for ln in _running_config_lines() if ln.startswith("ip dhcp host")]
    if not rows:
        return "No static DHCP reservations."
    out = ["# Static DHCP reservations", "| MAC | IP |", "|---|---|"]
    for r in rows:
        parts = r.split()  # ip dhcp host <mac> <ip>
        if len(parts) >= 5:
            out.append(f"| {parts[3]} | {parts[4]} |")
    return "\n".join(out)


@mcp.tool()
def set_static_lease(mac: str, ip: str, name: str = "") -> str:
    """Assign a fixed IP to a MAC address (DHCP reservation) + save config.
    mac: aa:bb:cc:dd:ee:ff · ip: 192.168.1.x · name: optional dhcp-host label.
    NOTE: to change a device's DISPLAY name (web UI) use rename_device — this 'name'
    is only the internal label of the dhcp-host entry and does NOT set the known-host display name."""
    if not ENABLE_WRITES:
        return _WRITE_DISABLED_MSG
    host = {"mac": mac.lower(), "ip": ip}
    if name:
        host["name"] = name
    res = _rci([{"ip": {"dhcp": {"host": host}}}])
    save = _rci([{"system": {"configuration": {"save": {}}}}])
    errs = _status_errors(res) + _status_errors(save)
    if errs:
        return f"❌ set_static_lease FAILED ({ip} → {mac.lower()}): " + " | ".join(errs)
    return json.dumps({"ok": True, "mac": mac.lower(), "ip": ip}, ensure_ascii=False)


@mcp.tool()
def pin_device(identifier: str, ip: str = "") -> str:
    """Convert an active device's CURRENT IP into a static reservation in one step (no need to know the MAC).
    identifier: the device's IP, MAC, or a name fragment — looked up in the active DHCP lease table.
    ip: optional — pass a different IP to pin to. If empty, the device's current IP is used.
    Examples: pin_device("192.168.1.66") · pin_device("Raspberry Pi") · pin_device("PS5", "192.168.1.154")
    Guards: multiple matches, the target IP being active/reserved on another device, and write errors are all blocked."""
    if not ENABLE_WRITES:
        return _WRITE_DISABLED_MSG
    dev, leases, err = _resolve_device(identifier)
    if err:
        return err
    mac = str(dev.get("mac", "")).strip().lower()
    target_ip = ip.strip() or str(dev.get("ip", "")).strip()
    name = str(dev.get("name") or dev.get("hostname") or "").strip()
    if not mac or not target_ip:
        return f"❌ Device is missing mac/ip info, use set_static_lease manually: {dev}"
    # Conflict 1: is the target IP currently ACTIVE on another device (dynamic lease)?
    active_ip_to_mac = {str(l.get("ip", "")).strip(): str(l.get("mac", "")).strip().lower() for l in leases}
    if active_ip_to_mac.get(target_ip, mac) != mac:
        return (f"⚠️ {target_ip} is currently active on another device ({active_ip_to_mac[target_ip]}). "
                f"That device must release the IP first, or pass a different 'ip'.")
    # Conflict 2: is the target IP already statically reserved to a different MAC?
    for ln in _running_config_lines():
        if ln.startswith("ip dhcp host"):
            parts = ln.split()
            if len(parts) >= 5 and parts[4] == target_ip and parts[3].lower() != mac:
                return (f"⚠️ {target_ip} is already statically reserved to another MAC ({parts[3]}). "
                        f"Remove it with remove_static_lease or pass a different 'ip'.")
    host = {"mac": mac, "ip": target_ip}
    if name:
        host["name"] = name
    res = _rci([{"ip": {"dhcp": {"host": host}}}])
    save = _rci([{"system": {"configuration": {"save": {}}}}])
    errs = _status_errors(res) + _status_errors(save)
    if errs:
        return f"❌ Pin FAILED ({target_ip} → {mac}): " + " | ".join(errs)
    return json.dumps({"pinned": {"mac": mac, "ip": target_ip, "name": name}}, ensure_ascii=False)


@mcp.tool()
def rename_device(identifier: str, name: str) -> str:
    """Give a device a persistent DISPLAY name on the router (Keenetic 'known host' entry = the device name in the web UI).
    identifier: the device's IP, MAC, or current name fragment (looked up in the active lease table).
    name: the new display name (spaces allowed; double quotes and non-printable characters are stripped).
    NOTE: set_static_lease / pin_device do NOT change the DISPLAY name — the display name is set ONLY by this tool.
    Examples: rename_device("192.168.1.66", "Pi5 Media") · rename_device("PS5", "PlayStation 5 Living Room")"""
    if not ENABLE_WRITES:
        return _WRITE_DISABLED_MSG
    new = "".join(c for c in name if c.isprintable()).replace('"', "").strip()
    if not new:
        return "❌ name is invalid/empty (nothing left after stripping double quotes and control characters)."
    dev, leases, err = _resolve_device(identifier)
    if err:
        return err
    mac = str(dev.get("mac", "")).strip().lower()
    if not mac:
        return f"❌ Could not read the device's MAC: {dev}"
    old = str(dev.get("name") or dev.get("hostname") or "?")
    # Keenetic: `known host "<name>" <mac>` — raw CLI via the parse endpoint
    res = _rci([{"parse": f'known host "{new}" {mac}'}])
    save = _rci([{"system": {"configuration": {"save": {}}}}])
    errs = _status_errors(res) + _status_errors(save)
    if errs:
        return f"❌ Rename FAILED ({mac}): " + " | ".join(errs)
    return json.dumps({"renamed": {"mac": mac, "old": old, "new": new}}, ensure_ascii=False)


@mcp.tool()
def find_free_ip(start: int = 100, end: int = 149) -> str:
    """Suggest a free IP for a new static reservation (from the .start-.end range, outside the dynamic pool).
    Active leases + existing static reservations + gateway(.1)/broadcast(.255) are excluded; up to 5 free IPs returned.
    start/end: the last-octet range to scan (default .100-.149). Adjust to match your router's dynamic pool."""
    if start > end:
        start, end = end, start
    leases = _active_leases()
    prefix, gateway = _lan_prefix(leases)
    used = set(str(l.get("ip", "")).strip() for l in leases if l.get("ip"))
    used |= _static_lease_ips()
    used.add(f"{prefix}.1")    # gateway
    used.add(f"{prefix}.255")  # broadcast
    if gateway:
        used.add(gateway)
    free = []
    for n in range(max(2, start), min(254, end) + 1):  # exclude .1 (gateway) and .0/.255
        cand = f"{prefix}.{n}"
        if cand not in used:
            free.append(cand)
        if len(free) >= 5:
            break
    if not free:
        return f"❌ No free IPs in {prefix}.{start}-{end} (all taken by lease/static/reserved)."
    return "Free IP candidates (for a new static reservation): " + ", ".join(free)


@mcp.tool()
def remove_static_lease(mac: str) -> str:
    """Remove a MAC's static IP reservation + save config."""
    if not ENABLE_WRITES:
        return _WRITE_DISABLED_MSG
    res = _rci([{"no": {"ip": {"dhcp": {"host": {"mac": mac.lower()}}}}}])
    save = _rci([{"system": {"configuration": {"save": {}}}}])
    errs = _status_errors(res) + _status_errors(save)
    if errs:
        return f"❌ remove_static_lease FAILED ({mac.lower()}): " + " | ".join(errs)
    return json.dumps({"removed": mac.lower()}, ensure_ascii=False)


@mcp.tool()
def wan_status() -> str:
    """Internet/WAN status + system uptime/load. Whether there's a connection, the WAN IP, uptime."""
    sysinfo = _rci([{"show": {"system": {}}}])[0]["show"]["system"]
    ifaces = _rci([{"show": {"interface": {}}}])[0]["show"]["interface"]
    if isinstance(ifaces, dict):
        items = ifaces.items()
    else:
        items = [(i.get("id", "?"), i) for i in ifaces]
    wan = []
    for name, i in items:
        if not isinstance(i, dict):
            continue
        if i.get("global") or i.get("defaultgw") or str(i.get("role", "")).find("internet") >= 0:
            wan.append(f"- {name}: connected={i.get('connected','?')} link={i.get('link','?')} "
                       f"addr={i.get('address','-')} desc={i.get('description','')}")
    out = ["# WAN / system status",
           f"uptime: {sysinfo.get('uptime','?')} s · cpuload: {sysinfo.get('cpuload','?')}% · "
           f"memory: {sysinfo.get('memory','?')} · model: {sysinfo.get('hw_id') or sysinfo.get('description','?')}",
           "## Internet interfaces:"]
    out += wan or ["(no global/internet interface found — raw 'show interface' output may be needed)"]
    return "\n".join(out)


@mcp.tool()
def list_port_forwards() -> str:
    """List port-forwarding / static NAT rules (running-config 'ip static' lines, READ-ONLY)."""
    rows = [ln for ln in _running_config_lines() if ln.startswith("ip static")]
    if not rows:
        return "No port-forward / static NAT rules."
    return "# Port forwards / static NAT (ip static)\n" + "\n".join(f"- {r}" for r in rows)


@mcp.tool()
def reboot(confirm: bool = False) -> str:
    """Reboot the router. GUARD: only runs when confirm=True (internet drops for ~1-2 minutes)."""
    if not ENABLE_WRITES:
        return _WRITE_DISABLED_MSG
    if not confirm:
        return "⚠️ Reboot cancelled — pass confirm=True to proceed. (Internet drops for 1-2 minutes.)"
    res = _rci([{"system": {"reboot": {}}}])
    return "Reboot triggered: " + json.dumps(res, ensure_ascii=False)[:300]


if __name__ == "__main__":
    if not PASS:
        raise SystemExit("KEENETIC_PASS is not set. Copy .env.example to .env and fill it in.")
    mcp.run(transport="streamable-http")
