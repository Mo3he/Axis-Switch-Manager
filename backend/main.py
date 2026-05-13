"""
Axis Switch Central Manager - FastAPI Backend
Proxies requests to Axis network switches and provides a unified API.
"""

import asyncio
import ipaddress
import json
import random
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Axis Switch Manager", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SWITCHES_FILE = Path(__file__).parent / "switches.json"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def load_switches() -> list[dict]:
    if SWITCHES_FILE.exists():
        try:
            return json.loads(SWITCHES_FILE.read_text())
        except Exception:
            return []
    return []


def save_switches(switches: list[dict]) -> None:
    SWITCHES_FILE.write_text(json.dumps(switches, indent=2))


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SwitchCreate(BaseModel):
    name: str
    ip: str
    username: str
    password: str


class SwitchUpdate(BaseModel):
    name: str | None = None
    ip: str | None = None
    username: str | None = None
    password: str | None = None


# ---------------------------------------------------------------------------
# Switch session helper
# ---------------------------------------------------------------------------

def _make_cookies() -> dict:
    """Return fresh random session cookies for a switch login."""
    return {
        "cid": str(random.randint(100_000_000, 999_999_999)),
        "seid": str(random.randint(100_000_000, 999_999_999)),
        "fromloginpage": "true",
    }


@asynccontextmanager
async def _switch_session(switch: dict):
    """
    Async context manager: opens a fresh connection, logs in, yields the
    authenticated client, then always closes it on exit.
    Switches support very few concurrent sessions - we never hold them open.
    """
    client = httpx.AsyncClient(
        base_url=f"http://{switch['ip']}",
        timeout=10.0,
        follow_redirects=True,
    )
    try:
        cookies = _make_cookies()
        try:
            resp = await client.post(
                "/config/login",
                data={"username": switch["username"], "password": switch["password"]},
                cookies=cookies,
            )
            if resp.status_code not in (200, 302):
                raise HTTPException(status_code=502, detail=f"Login failed for {switch['ip']}")
            client.cookies.update(cookies)
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail=f"Cannot connect to {switch['ip']}")
        yield client
    finally:
        try:
            await client.get("/config/logout")
        except Exception:
            pass
        await client.aclose()


# Keep _get_client and _fetch as thin wrappers so existing POST endpoints
# work with minimal change - they now get a fresh client every call.

async def _get_client(switch: dict) -> httpx.AsyncClient:
    """
    Returns a fresh authenticated client. Caller MUST call aclose() when done.
    Prefer using _switch_session() context manager instead.
    """
    client = httpx.AsyncClient(
        base_url=f"http://{switch['ip']}",
        timeout=10.0,
        follow_redirects=True,
    )
    cookies = _make_cookies()
    try:
        resp = await client.post(
            "/config/login",
            data={"username": switch["username"], "password": switch["password"]},
            cookies=cookies,
        )
        if resp.status_code not in (200, 302):
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"Login failed for {switch['ip']}")
        client.cookies.update(cookies)
    except httpx.ConnectError:
        await client.aclose()
        raise HTTPException(status_code=503, detail=f"Cannot connect to {switch['ip']}")
    return client


async def _fetch(switch: dict, path: str) -> str:
    """Fetch a stat/config path from a switch, always closing the connection."""
    async with _switch_session(switch) as client:
        try:
            resp = await client.get(f"/{path}")
            if resp.status_code != 200 or resp.text.startswith("<!DOCTYPE"):
                return ""
            return resp.text
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc))


# ---------------------------------------------------------------------------
# Data parsers
# ---------------------------------------------------------------------------

def parse_sys_overview(raw: str) -> dict:
    """Parse the `stat/sys_overview` response."""
    result = {}
    if not raw:
        return result
    for segment in raw.split("!"):
        segment = segment.strip()
        if "^" in segment:
            key, _, value = segment.partition("^")
            key = key.strip()
            if key:
                result[key] = value.strip().replace("%20", " ")
    return result


def parse_ports(raw: str) -> list[dict]:
    """Parse the `stat/ports` traffic counters response."""
    if not raw:
        return []
    ports = []
    for entry in raw.strip().split("|"):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split("/")
        if len(fields) >= 9:
            ports.append({
                "port": int(fields[0]),
                "rx_packets": int(fields[1]),
                "tx_packets": int(fields[2]),
                "rx_bytes": int(fields[3]),
                "tx_bytes": int(fields[4]),
                "rx_errors": int(fields[5]),
                "tx_errors": int(fields[6]),
                "rx_drops": int(fields[7]),
                "tx_drops": int(fields[8]),
                "collisions": int(fields[9]) if len(fields) > 9 else 0,
            })
    return ports


def parse_port_config(raw: str) -> list[dict]:
    """
    Parse the `config/ports` response.
    Fields: portId/caps/adminEnabled/autoNeg/speed/duplex/maxFrameSize/flowCtrl
            /poe/linkState/speed/...
    """
    if not raw:
        return []
    ports = []
    for entry in raw.strip().split("|"):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split("/")
        if len(fields) >= 11:
            ports.append({
                "port": int(fields[0]),
                "admin_enabled": fields[2] == "1",
                "auto_neg": fields[3] == "1",
                "link_state": fields[9],
                "speed": fields[10],
                "flow_ctrl": fields[7] == "1",
                "max_frame": int(fields[6]) if fields[6].isdigit() else 9600,
            })
    return ports


def parse_poe_status(raw: str) -> list[dict]:
    """
    Parse the `stat/poe_status` response.
    Fields: portId/maxPower/currentPower/voltage/current/status/class/priority
    """
    if not raw:
        return []
    ports = []
    for entry in raw.strip().split("|"):
        entry = entry.strip()
        if not entry:
            continue
        # Strip leading non-digit characters (e.g. '?')
        if entry[0] == "?":
            entry = entry[1:]
        fields = entry.split("/")
        if len(fields) >= 7:
            ports.append({
                "port": int(fields[0]),
                "max_power": int(fields[1]) / 10.0,
                "current_power": int(fields[2]) / 10.0,
                "voltage": int(fields[3]) / 10.0,
                "current_ma": int(fields[4]),
                "status": fields[5],
                "poe_class": fields[6] if fields[6] != "-" else None,
            })
    return ports


def merge_port_data(config: list[dict], traffic: list[dict], poe: list[dict]) -> list[dict]:
    """Merge port config, traffic stats and PoE data into one list."""
    poe_by_port = {p["port"]: p for p in poe}
    traffic_by_port = {p["port"]: p for p in traffic}
    result = []
    for cfg in config:
        p = cfg["port"]
        merged = {**cfg}
        if p in traffic_by_port:
            merged.update(traffic_by_port[p])
        if p in poe_by_port:
            merged["poe"] = poe_by_port[p]
        result.append(merged)
    return result


# ---------------------------------------------------------------------------
# Switch inventory endpoints
# ---------------------------------------------------------------------------

@app.get("/api/switches")
async def list_switches():
    switches = load_switches()
    return [{"id": s["id"], "name": s["name"], "ip": s["ip"], "username": s["username"]} for s in switches]


@app.post("/api/switches", status_code=201)
async def add_switch(payload: SwitchCreate):
    switches = load_switches()
    new_id = str(int(time.time() * 1000))
    switch = {
        "id": new_id,
        "name": payload.name,
        "ip": payload.ip,
        "username": payload.username,
        "password": payload.password,
    }
    switches.append(switch)
    save_switches(switches)
    return {"id": new_id, "name": switch["name"], "ip": switch["ip"]}


@app.put("/api/switches/{switch_id}")
async def update_switch(switch_id: str, payload: SwitchUpdate):
    switches = load_switches()
    for sw in switches:
        if sw["id"] == switch_id:
            if payload.name is not None:
                sw["name"] = payload.name
            if payload.ip is not None:
                sw["ip"] = payload.ip
            if payload.username is not None:
                sw["username"] = payload.username
            if payload.password is not None:
                sw["password"] = payload.password
            save_switches(switches)
            # Invalidate cached client
            if switch_id in _http_clients:
                await _http_clients[switch_id].aclose()
                del _http_clients[switch_id]
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Switch not found")


@app.delete("/api/switches/{switch_id}")
async def delete_switch(switch_id: str):
    switches = load_switches()
    new_list = [s for s in switches if s["id"] != switch_id]
    if len(new_list) == len(switches):
        raise HTTPException(status_code=404, detail="Switch not found")
    save_switches(new_list)
    if switch_id in _http_clients:
        await _http_clients[switch_id].aclose()
        del _http_clients[switch_id]
    return {"ok": True}


# ---------------------------------------------------------------------------
# Per-switch data endpoints
# ---------------------------------------------------------------------------

def _find_switch(switch_id: str) -> dict:
    for sw in load_switches():
        if sw["id"] == switch_id:
            return sw
    raise HTTPException(status_code=404, detail="Switch not found")


@app.get("/api/switches/{switch_id}/overview")
async def switch_overview(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "stat/sys_overview")
    return parse_sys_overview(raw)


@app.get("/api/switches/{switch_id}/ports")
async def switch_ports(switch_id: str):
    sw = _find_switch(switch_id)
    config_raw, traffic_raw, poe_raw = await asyncio.gather(
        _fetch(sw, "config/ports"),
        _fetch(sw, "stat/ports"),
        _fetch(sw, "stat/poe_status"),
    )
    config = parse_port_config(config_raw)
    traffic = parse_ports(traffic_raw)
    poe = parse_poe_status(poe_raw)
    return merge_port_data(config, traffic, poe)


@app.get("/api/switches/{switch_id}/poe")
async def switch_poe(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "stat/poe_status")
    return parse_poe_status(raw)


@app.get("/api/switches/{switch_id}/traffic")
async def switch_traffic(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "stat/ports")
    return parse_ports(raw)


# ---------------------------------------------------------------------------
# Dashboard - aggregate all switches
# ---------------------------------------------------------------------------

@app.get("/api/dashboard")
async def dashboard():
    switches = load_switches()
    results = []
    for sw in switches:
        try:
            raw = await _fetch(sw, "stat/sys_overview")
            overview = parse_sys_overview(raw)
            poe_raw = await _fetch(sw, "stat/poe_status")
            poe = parse_poe_status(poe_raw)
            active_ports = sum(1 for p in poe if "ON" in p.get("status", ""))
            total_poe_w = sum(p["current_power"] for p in poe)
            results.append({
                "id": sw["id"],
                "name": sw["name"],
                "ip": sw["ip"],
                "status": "online",
                "overview": overview,
                "active_poe_ports": active_ports,
                "total_poe_watts": round(total_poe_w, 1),
                "poe_ports": len(poe),
            })
        except HTTPException:
            results.append({
                "id": sw["id"],
                "name": sw["name"],
                "ip": sw["ip"],
                "status": "offline",
                "overview": {},
                "active_poe_ports": 0,
                "total_poe_watts": 0,
                "poe_ports": 0,
            })
    return results


# ---------------------------------------------------------------------------
# Port control
# ---------------------------------------------------------------------------

class PortControlPayload(BaseModel):
    port: int
    admin_enabled: bool


@app.post("/api/switches/{switch_id}/ports/control")
async def port_control(switch_id: str, payload: PortControlPayload):
    """Enable or disable a port (admin state)."""
    sw = _find_switch(switch_id)
    config_raw = await _fetch(sw, "config/ports")
    ports = parse_port_config(config_raw)

    rows = []
    for p in ports:
        if p["port"] == payload.port:
            admin = "1" if payload.admin_enabled else "0"
        else:
            admin = "1" if p["admin_enabled"] else "0"
        rows.append(f"{p['port']}/{admin}")

    async with _switch_session(sw) as client:
        try:
            await client.post("/config/ports", data={"port_data": "|".join(rows)})
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# System info config (GET + POST)
# ---------------------------------------------------------------------------

def parse_sysinfo(raw: str) -> dict:
    """Parse config/sysinfo response: contact,name,location,http_access,https_access"""
    if not raw:
        return {}
    parts = raw.strip().rstrip("%").split(",")
    return {
        "sys_contact": parts[0].replace("%20", " ").strip() if len(parts) > 0 else "",
        "sys_name": parts[1].replace("%20", " ").strip() if len(parts) > 1 else "",
        "sys_location": parts[2].replace("%20", " ").strip() if len(parts) > 2 else "",
    }


@app.get("/api/switches/{switch_id}/config/system")
async def get_system_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/sysinfo")
    return parse_sysinfo(raw)


class SystemConfigPayload(BaseModel):
    sys_name: str | None = None
    sys_contact: str | None = None
    sys_location: str | None = None


@app.post("/api/switches/{switch_id}/config/system")
async def set_system_config(switch_id: str, payload: SystemConfigPayload):
    sw = _find_switch(switch_id)
    # Read current values first
    current_raw = await _fetch(sw, "config/sysinfo")
    current = parse_sysinfo(current_raw)
    name = payload.sys_name if payload.sys_name is not None else current.get("sys_name", "")
    contact = payload.sys_contact if payload.sys_contact is not None else current.get("sys_contact", "")
    location = payload.sys_location if payload.sys_location is not None else current.get("sys_location", "")
    async with _switch_session(sw) as client:
        try:
            await client.post(
                "/config/sysinfo",
                data={
                    "sys_contact": contact,
                    "sys_name": name,
                    "sys_location": location,
                },
            )
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# PoE port config (GET + POST)
# ---------------------------------------------------------------------------

def parse_poe_config(raw: str) -> dict:
    """
    Parse config/poe_config response.
    Format: mode|global_section|port1,port2,...|...
    Per port: portId/admin/maxPower/priority/mode/...
    admin: 0=disabled, 1=enabled(PoH), 2=enabled
    priority: 1=low, 2=high, 3=critical
    """
    if not raw:
        return {"ports": []}
    raw = raw.strip().rstrip("%")
    parts = raw.split("|")
    port_section = parts[2] if len(parts) > 2 else ""
    ports = []
    for entry in port_section.split(","):
        entry = entry.strip()
        if not entry:
            continue
        fields = entry.split("/")
        if len(fields) >= 3:
            ports.append({
                "port": int(fields[0]),
                "poe_enabled": fields[1] != "0",
                "max_power_w": int(fields[2]) / 10.0 if fields[2].lstrip("-").isdigit() else 30.0,
                "priority": int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else 2,
            })
    return {"ports": ports}


@app.get("/api/switches/{switch_id}/config/poe")
async def get_poe_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/poe_config")
    return parse_poe_config(raw)


class PoePortConfig(BaseModel):
    port: int
    poe_enabled: bool
    priority: int = 2  # 1=low, 2=high, 3=critical


class PoeConfigPayload(BaseModel):
    ports: list[PoePortConfig]


@app.post("/api/switches/{switch_id}/config/poe")
async def set_poe_config(switch_id: str, payload: PoeConfigPayload):
    sw = _find_switch(switch_id)
    # Read current full config to preserve untouched ports and global settings
    raw = await _fetch(sw, "config/poe_config")
    raw = raw.strip().rstrip("%")
    parts = raw.split("|")
    # Rebuild port section from current + patches
    current_cfg = parse_poe_config(raw)
    patch_by_port = {p.port: p for p in payload.ports}
    new_port_entries = []
    for p in current_cfg["ports"]:
        pn = patch_by_port.get(p["port"], None)
        if pn:
            enabled = "2" if pn.poe_enabled else "0"
            priority = str(pn.priority)
        else:
            enabled = "2" if p["poe_enabled"] else "0"
            priority = str(p["priority"])
        max_pw = int(p["max_power_w"] * 10)
        new_port_entries.append(f"{p['port']}/{enabled}/{max_pw}/{priority}/0/-1/0/0/0/0")
    # Rebuild full payload preserving other sections
    parts[2] = ",".join(new_port_entries)
    new_raw = "|".join(parts)
    async with _switch_session(sw) as client:
        try:
            resp = await client.post(
                "/config/poe_config",
                data={"PoeData": new_raw},
            )
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Port config (admin enable/disable, speed) POST
# ---------------------------------------------------------------------------

class PortsConfigPayload(BaseModel):
    ports: list[dict]  # [{port, admin_enabled, ...}]


@app.post("/api/switches/{switch_id}/config/ports")
async def set_ports_config(switch_id: str, payload: PortsConfigPayload):
    sw = _find_switch(switch_id)
    config_raw = await _fetch(sw, "config/ports")
    current = parse_port_config(config_raw)
    patch = {p["port"]: p for p in payload.ports}
    # Re-build the pipe-delimited string preserving all fields
    rows = []
    for p in current:
        pp = patch.get(p["port"], {})
        fields = config_raw.split("|")[p["port"] - 1].split("/")
        if "admin_enabled" in pp:
            fields[2] = "1" if pp["admin_enabled"] else "0"
        rows.append("/".join(fields))
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/ports", data={"portData": "|".join(rows)})
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Network scanner
# ---------------------------------------------------------------------------

class ScanPayload(BaseModel):
    subnet: str          # e.g. "10.129.174.0/24" or "10.129.174.1-50"
    username: str = "root"
    password: str = ""
    timeout: float = 2.0


def _expand_targets(subnet: str) -> list[str]:
    """Expand CIDR or range notation into a list of IP strings."""
    subnet = subnet.strip()
    # Range: 10.0.0.1-50 or 10.0.0.1-10.0.0.50
    if "-" in subnet and "/" not in subnet:
        parts = subnet.split("-")
        base = parts[0].strip()
        end_part = parts[1].strip()
        if "." in end_part:
            # Full IP range
            start = int(ipaddress.IPv4Address(base))
            end = int(ipaddress.IPv4Address(end_part))
        else:
            # Short range: 10.0.0.1-50
            prefix = ".".join(base.split(".")[:3])
            start = int(ipaddress.IPv4Address(base))
            end = int(ipaddress.IPv4Address(f"{prefix}.{end_part}"))
        return [str(ipaddress.IPv4Address(i)) for i in range(start, end + 1)]
    # CIDR
    try:
        net = ipaddress.IPv4Network(subnet, strict=False)
        # Cap to 1024 hosts to avoid accidental huge scans
        hosts = list(net.hosts())[:1024]
        return [str(h) for h in hosts]
    except ValueError:
        # Single IP
        return [subnet]


async def _probe_ip(ip: str, timeout: float) -> dict | None:
    """Return switch info dict if ip is an Axis switch, else None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"http://{ip}/lib/config.js")
            if resp.status_code != 200:
                return None
            text = resp.text
            # Fingerprint: must have configVendor = "AXIS"
            if 'configVendor = "AXIS"' not in text:
                return None
            # Extract fields
            def _js_var(name: str) -> str:
                m = re.search(rf'var {name}\s*=\s*"([^"]+)"', text)
                return m.group(1) if m else ""
            def _js_int(name: str) -> str:
                m = re.search(rf'var {name}\s*=\s*(\d+)', text)
                return m.group(1) if m else "0"
            return {
                "ip": ip,
                "model": _js_var("configSwitchName"),
                "description": _js_var("configSwitchDescription"),
                "platform": _js_var("configPlatformName"),
                "mac": _js_var("configMac"),
                "port_count": int(_js_int("configNormalPortMax")),
            }
    except Exception:
        return None


@app.post("/api/scan")
async def scan_network(payload: ScanPayload):
    """Scan a subnet for Axis switches. Returns list of discovered switches."""
    try:
        targets = _expand_targets(payload.subnet)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid subnet: {e}")

    # Limit scan size
    if len(targets) > 1024:
        raise HTTPException(status_code=400, detail="Subnet too large (max 1024 hosts)")

    # Probe concurrently with a semaphore to avoid overwhelming the network
    sem = asyncio.Semaphore(50)

    async def _guarded_probe(ip: str) -> dict | None:
        async with sem:
            return await _probe_ip(ip, payload.timeout)

    results = await asyncio.gather(*[_guarded_probe(ip) for ip in targets])
    found = [r for r in results if r is not None]

    # Mark which IPs are already in inventory
    existing_ips = {sw["ip"] for sw in load_switches()}
    for item in found:
        item["already_added"] = item["ip"] in existing_ips

    return {"found": found, "scanned": len(targets)}


# ---------------------------------------------------------------------------
# Bulk add switches from scan results
# ---------------------------------------------------------------------------

class BulkAddEntry(BaseModel):
    name: str
    ip: str
    username: str
    password: str


class BulkAddPayload(BaseModel):
    switches: list[BulkAddEntry]


@app.post("/api/switches/bulk-add", status_code=201)
async def bulk_add_switches(payload: BulkAddPayload):
    switches = load_switches()
    existing_ips = {sw["ip"] for sw in switches}
    added = []
    skipped = []
    for entry in payload.switches:
        if entry.ip in existing_ips:
            skipped.append(entry.ip)
            continue
        new_id = str(int(time.time() * 1000) + len(added))
        sw = {
            "id": new_id,
            "name": entry.name,
            "ip": entry.ip,
            "username": entry.username,
            "password": entry.password,
        }
        switches.append(sw)
        existing_ips.add(entry.ip)
        added.append({"id": new_id, "ip": entry.ip, "name": entry.name})
    save_switches(switches)
    return {"added": added, "skipped": skipped}


# ---------------------------------------------------------------------------
# NTP config (GET + POST)
# ---------------------------------------------------------------------------

def parse_ntp(raw: str) -> dict:
    """
    Parse config/ntp response.
    Format: ipv6_supported/mode/interval/server1/server2/.../server5/auto_mode/auto_server
    """
    if not raw:
        return {}
    parts = raw.strip().rstrip("%").split("/")
    return {
        "mode": int(parts[1]) if len(parts) > 1 else 0,  # 0=disabled,1=enabled
        "interval": int(parts[2]) if len(parts) > 2 else 3600,
        "server1": parts[3] if len(parts) > 3 else "",
        "server2": parts[4] if len(parts) > 4 else "",
        "server3": parts[5] if len(parts) > 5 else "",
        "server4": parts[6] if len(parts) > 6 else "",
        "server5": parts[7] if len(parts) > 7 else "",
    }


@app.get("/api/switches/{switch_id}/config/ntp")
async def get_ntp_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/ntp")
    return parse_ntp(raw)


class NtpConfigPayload(BaseModel):
    mode: int = 1          # 0=disabled, 1=enabled
    interval: int = 3600
    server1: str = ""
    server2: str = ""
    server3: str = ""
    server4: str = ""
    server5: str = ""


@app.post("/api/switches/{switch_id}/config/ntp")
async def set_ntp_config(switch_id: str, payload: NtpConfigPayload):
    sw = _find_switch(switch_id)
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/ntp", data={
                "ntp_mode": str(payload.mode),
                "ntp_polling_interval": str(payload.interval),
                "ntp_server1": payload.server1,
                "ntp_server2": payload.server2,
                "ntp_server3": payload.server3,
                "ntp_server4": payload.server4,
                "ntp_server5": payload.server5,
            })
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Port descriptions (GET + POST)
# ---------------------------------------------------------------------------

def parse_ports_desc(raw: str) -> list[dict]:
    """
    Parse config/ports_desc response.
    Format: portId/description|portId/description|...
    """
    if not raw:
        return []
    ports = []
    for entry in raw.strip().split("|"):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split("/", 1)
        if len(parts) >= 1 and parts[0].isdigit():
            ports.append({
                "port": int(parts[0]),
                "description": parts[1] if len(parts) > 1 else "",
            })
    return ports


@app.get("/api/switches/{switch_id}/config/ports_desc")
async def get_ports_desc(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/ports_desc")
    return parse_ports_desc(raw)


class PortDesc(BaseModel):
    port: int
    description: str


class PortsDescPayload(BaseModel):
    ports: list[PortDesc]


@app.post("/api/switches/{switch_id}/config/ports_desc")
async def set_ports_desc(switch_id: str, payload: PortsDescPayload):
    sw = _find_switch(switch_id)
    # Read current to preserve ports not in payload
    raw = await _fetch(sw, "config/ports_desc")
    current = {p["port"]: p["description"] for p in parse_ports_desc(raw)}
    patch = {p.port: p.description for p in payload.ports}
    current.update(patch)
    form_data = {f"desc_{port}": desc for port, desc in sorted(current.items())}
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/ports_desc", data=form_data)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Loop protection config (GET + POST)
# ---------------------------------------------------------------------------

def parse_loop_config(raw: str) -> dict:
    """
    Parse config/loop_config response.
    Format: global_enable,tx_interval,shutdown_time,port_section
    port_section: portId/enable/action/txmode|...
    action: 0=shutdown, 1=shutdown+log, 2=log only
    """
    if not raw:
        return {"global_enable": False, "tx_interval": 5, "shutdown_time": 180, "ports": []}
    raw = raw.strip().rstrip("%")
    parts = raw.split(",", 3)
    global_enable = parts[0] == "1" if len(parts) > 0 else False
    tx_interval = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 5
    shutdown_time = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 180
    ports = []
    if len(parts) > 3:
        for entry in parts[3].split("|"):
            entry = entry.strip()
            if not entry:
                continue
            f = entry.split("/")
            if len(f) >= 4 and f[0].isdigit():
                ports.append({
                    "port": int(f[0]),
                    "enable": f[1] == "1",
                    "action": int(f[2]),   # 0=shutdown, 1=shutdown+log, 2=log only
                    "tx_mode": f[3] == "1",
                })
    return {
        "global_enable": global_enable,
        "tx_interval": tx_interval,
        "shutdown_time": shutdown_time,
        "ports": ports,
    }


@app.get("/api/switches/{switch_id}/config/loop")
async def get_loop_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/loop_config")
    return parse_loop_config(raw)


class LoopPortConfig(BaseModel):
    port: int
    enable: bool = True
    action: int = 0    # 0=shutdown, 1=shutdown+log, 2=log only
    tx_mode: bool = True


class LoopConfigPayload(BaseModel):
    global_enable: bool = True
    tx_interval: int = 5
    shutdown_time: int = 180
    ports: list[LoopPortConfig] = []


@app.post("/api/switches/{switch_id}/config/loop")
async def set_loop_config(switch_id: str, payload: LoopConfigPayload):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/loop_config")
    current = parse_loop_config(raw)
    patch = {p.port: p for p in payload.ports}
    form_data: dict = {
        "gbl_enable": "1" if payload.global_enable else "0",
        "tx_time": str(payload.tx_interval),
        "shutdown_time": str(payload.shutdown_time),
    }
    for p in current["ports"]:
        pp = patch.get(p["port"])
        enable = pp.enable if pp else p["enable"]
        action = pp.action if pp else p["action"]
        tx_mode = pp.tx_mode if pp else p["tx_mode"]
        form_data[f"enable_{p['port']}"] = "on" if enable else ""
        form_data[f"action_{p['port']}"] = str(action)
        form_data[f"txmode_{p['port']}"] = str(int(tx_mode))
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/loop_config", data=form_data)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# VLAN config (GET only - display, POST is complex)
# ---------------------------------------------------------------------------

def parse_vlan(raw: str) -> dict:
    """
    Parse config/vlan response.
    Format: tpid#vlans_list#port1_config#port2_config...
    Per port: mode/pvid/frame_type/ingress_filter/tx_tag/...
    mode: 0=access, 1=trunk, 2=hybrid
    """
    if not raw:
        return {"tpid": "88A8", "ports": []}
    raw = raw.strip().rstrip("%")
    parts = raw.split("#")
    tpid = parts[0] if len(parts) > 0 else "88A8"
    ports = []
    for i, entry in enumerate(parts[2:], start=1):
        if not entry:
            continue
        f = entry.rstrip("/").split("/")
        if len(f) >= 5:
            mode_map = {0: "Access", 1: "Trunk", 2: "Hybrid"}
            mode = int(f[0]) if f[0].isdigit() else 0
            ports.append({
                "port": i,
                "mode": mode,
                "mode_name": mode_map.get(mode, "Access"),
                "pvid": int(f[1]) if len(f) > 1 and f[1].isdigit() else 1,
                "frame_type": int(f[2]) if len(f) > 2 and f[2].isdigit() else 0,
                "ingress_filter": f[3] == "1" if len(f) > 3 else False,
                "tx_tag": int(f[4]) if len(f) > 4 and f[4].isdigit() else 0,
                "allowed_vlans": f[10] if len(f) > 10 else "1-4095",
            })
    return {"tpid": tpid.upper(), "ports": ports}


@app.get("/api/switches/{switch_id}/config/vlan")
async def get_vlan_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/vlan")
    return parse_vlan(raw)


class VlanPortConfig(BaseModel):
    port: int
    mode: int = 0          # 0=access, 1=trunk, 2=hybrid
    pvid: int = 1
    frame_type: int = 0    # 0=all, 1=tagged+untagged, 2=tagged only
    ingress_filter: bool = False
    tx_tag: int = 0        # 0=untag pvid, 2=tag all, 3=untag all
    allowed_vlans: str = "1-4095"


class VlanConfigPayload(BaseModel):
    tpid: str = "88A8"
    ports: list[VlanPortConfig]


@app.post("/api/switches/{switch_id}/config/vlan")
async def set_vlan_config(switch_id: str, payload: VlanConfigPayload):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/vlan")
    current = parse_vlan(raw)
    patch = {p.port: p for p in payload.ports}
    form_data: dict = {"tpid": payload.tpid}
    for p in current["ports"]:
        pp = patch.get(p["port"])
        mode = pp.mode if pp else p["mode"]
        pvid = pp.pvid if pp else p["pvid"]
        frame_type = pp.frame_type if pp else p["frame_type"]
        ingress_filter = pp.ingress_filter if pp else p["ingress_filter"]
        tx_tag = pp.tx_tag if pp else p["tx_tag"]
        allowed = pp.allowed_vlans if pp else p.get("allowed_vlans", "1-4095")
        form_data[f"mode_{p['port']}"] = str(mode)
        form_data[f"pvid_{p['port']}"] = str(pvid)
        form_data[f"frame_type_{p['port']}"] = str(frame_type)
        form_data[f"ingressflt_{p['port']}"] = "on" if ingress_filter else ""
        form_data[f"tx_tag_{p['port']}"] = str(tx_tag)
        form_data[f"allowed_{p['port']}"] = allowed
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/vlan", data=form_data)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Private VLAN (PVLAN) config (GET + POST)
# ---------------------------------------------------------------------------

def parse_pvlan(raw: str) -> dict:
    """
    Parse config/pvlan response.
    Format: pvlan_id,port1_mode/port2_mode/.../|
    mode per port: 1=promiscuous, 0=isolated
    """
    if not raw:
        return {"pvlan_id": 1, "ports": []}
    raw = raw.strip().rstrip("%").rstrip("|")
    parts = raw.split(",", 1)
    pvlan_id = int(parts[0]) if parts[0].isdigit() else 1
    ports = []
    if len(parts) > 1:
        port_vals = parts[1].rstrip("/").split("/")
        for i, v in enumerate(port_vals, start=1):
            if v.isdigit():
                ports.append({
                    "port": i,
                    "mode": int(v),  # 1=promiscuous, 0=isolated
                    "mode_name": "Promiscuous" if v == "1" else "Isolated",
                })
    return {"pvlan_id": pvlan_id, "ports": ports}


@app.get("/api/switches/{switch_id}/config/pvlan")
async def get_pvlan_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/pvlan")
    return parse_pvlan(raw)


class PvlanPortConfig(BaseModel):
    port: int
    mode: int = 1  # 1=promiscuous, 0=isolated


class PvlanConfigPayload(BaseModel):
    pvlan_id: int = 1
    ports: list[PvlanPortConfig]


@app.post("/api/switches/{switch_id}/config/pvlan")
async def set_pvlan_config(switch_id: str, payload: PvlanConfigPayload):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/pvlan")
    current = parse_pvlan(raw)
    patch = {p.port: p for p in payload.ports}
    form_data: dict = {"pvlan_id": str(payload.pvlan_id)}
    for p in current["ports"]:
        pp = patch.get(p["port"])
        mode = pp.mode if pp else p["mode"]
        form_data[f"pvlanport_{p['port']}"] = str(mode)
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/pvlan", data=form_data)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Link Aggregation config (GET + POST)
# ---------------------------------------------------------------------------

def parse_aggregation(raw: str) -> dict:
    """
    Parse config/aggregation response.
    Format: row1|row2|row3|...|global_section
    Row 0: port admin state 1/1/1/...
    Rows 1-5: aggregation group membership
    Last row: global config
    """
    if not raw:
        return {"groups": [], "ports": []}
    raw = raw.strip().rstrip("%")
    rows = raw.split("|")
    port_row = rows[0].split("/") if rows else []
    n_ports = len(port_row)
    groups = []
    for g_idx in range(1, min(6, len(rows))):
        members = rows[g_idx].split("/")
        group_ports = [i + 1 for i, v in enumerate(members) if v == "1"]
        if group_ports:
            groups.append({"group": g_idx, "ports": group_ports})
    return {
        "groups": groups,
        "port_count": n_ports,
    }


@app.get("/api/switches/{switch_id}/config/aggregation")
async def get_aggregation_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/aggregation")
    return parse_aggregation(raw)


# ---------------------------------------------------------------------------
# Port speed/duplex/flow config POST (extends existing config/ports)
# ---------------------------------------------------------------------------

class PortSpeedConfig(BaseModel):
    port: int
    admin_enabled: bool = True
    auto_neg: bool = True
    speed: str = "1Gfdx"   # e.g. "1Gfdx", "100fdx", "100hdx", "10fdx"
    flow_ctrl: bool = False
    max_frame: int = 9600


class PortsSpeedPayload(BaseModel):
    ports: list[PortSpeedConfig]


@app.post("/api/switches/{switch_id}/config/ports_speed")
async def set_ports_speed(switch_id: str, payload: PortsSpeedPayload):
    """Update port admin state, speed, duplex and flow control."""
    sw = _find_switch(switch_id)
    config_raw = await _fetch(sw, "config/ports")
    current = parse_port_config(config_raw)
    patch = {p.port: p for p in payload.ports}
    # Re-build pipe-delimited string preserving all original fields
    raw_rows = config_raw.split("|")
    new_rows = []
    for p in current:
        raw_fields = raw_rows[p["port"] - 1].split("/") if p["port"] - 1 < len(raw_rows) else []
        pp = patch.get(p["port"])
        if pp and raw_fields:
            raw_fields[2] = "1" if pp.admin_enabled else "0"
            raw_fields[3] = "1" if pp.auto_neg else "0"
            raw_fields[7] = "1" if pp.flow_ctrl else "0"
            raw_fields[6] = str(pp.max_frame)
            # Speed encoding: auto = current, manual needs speedVal
            # We pass the speed_ select value which the switch understands
            new_rows.append("/".join(raw_fields))
        elif raw_fields:
            new_rows.append("/".join(raw_fields))
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/ports", data={"portData": "|".join(new_rows)})
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# SNMP config (GET + POST)
# ---------------------------------------------------------------------------

def parse_snmp(raw: str) -> dict:
    """
    Parse config/snmp response.
    Format: mode,version,trap_host|community_entry|trap_entry
    Example: 1,1,None|,1/1/public/write/800000e53...|0/0/public//::/1/1/0/1/5/1//None
    """
    if not raw:
        return {"enabled": False, "version": 1, "community_ro": "public", "trap_host": ""}
    raw = raw.strip().rstrip("%")
    parts = raw.split("|")

    # Global section: mode,version,trap_host
    global_fields = parts[0].split(",") if parts else []
    enabled = global_fields[0] == "1" if global_fields else False
    version = int(global_fields[1]) if len(global_fields) > 1 and global_fields[1].isdigit() else 1
    trap_host_raw = global_fields[2] if len(global_fields) > 2 else "None"
    trap_host = "" if trap_host_raw == "None" else trap_host_raw

    # Community string: first non-empty entry after global
    community_ro = "public"
    for entry in parts[1:]:
        stripped = entry.lstrip(",")
        fields = stripped.split("/")
        if len(fields) >= 3 and fields[2]:
            community_ro = fields[2]
            break

    return {
        "enabled": enabled,
        "version": version,
        "community_ro": community_ro,
        "trap_host": trap_host,
        "_raw": raw,
    }


@app.get("/api/switches/{switch_id}/config/snmp")
async def get_snmp_config(switch_id: str):
    sw = _find_switch(switch_id)
    raw = await _fetch(sw, "config/snmp")
    return parse_snmp(raw)


class SnmpConfigPayload(BaseModel):
    enabled: bool = True
    version: int = 1           # 1 = SNMPv1, 2 = SNMPv2c
    community_ro: str = "public"
    trap_host: str = ""


@app.post("/api/switches/{switch_id}/config/snmp")
async def set_snmp_config(switch_id: str, payload: SnmpConfigPayload):
    sw = _find_switch(switch_id)
    # Fetch current raw to preserve all existing entries
    raw = await _fetch(sw, "config/snmp")
    raw = raw.strip().rstrip("%")
    parts = raw.split("|")

    # Rebuild global section
    global_fields = parts[0].split(",") if parts else ["1", "1", "None"]
    while len(global_fields) < 3:
        global_fields.append("None")
    global_fields[0] = "1" if payload.enabled else "0"
    global_fields[1] = str(payload.version)
    global_fields[2] = payload.trap_host if payload.trap_host else "None"

    # Update community string in all community entries
    new_parts = [",".join(global_fields)]
    for entry in parts[1:]:
        prefix = "," if entry.startswith(",") else ""
        stripped = entry.lstrip(",")
        fields = stripped.split("/")
        if len(fields) >= 3 and fields[2]:
            fields[2] = payload.community_ro
        new_parts.append(prefix + "/".join(fields))

    new_raw = "|".join(new_parts)
    async with _switch_session(sw) as client:
        try:
            await client.post("/config/snmp", data={"snmpData": new_raw})
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))


# ---------------------------------------------------------------------------
# Bulk config apply - apply same settings to multiple switches
# (placed after all payload classes are defined)
# ---------------------------------------------------------------------------

class BulkConfigPayload(BaseModel):
    switch_ids: list[str]
    system: SystemConfigPayload | None = None
    poe: PoeConfigPayload | None = None
    ports: PortsConfigPayload | None = None
    ntp: NtpConfigPayload | None = None
    loop: LoopConfigPayload | None = None
    ports_desc: PortsDescPayload | None = None
    snmp: SnmpConfigPayload | None = None


@app.post("/api/bulk/apply")
async def bulk_apply(payload: BulkConfigPayload):
    results = []
    for sw_id in payload.switch_ids:
        sw_result = {"id": sw_id, "ok": True, "errors": []}
        try:
            sw = _find_switch(sw_id)
        except HTTPException:
            results.append({"id": sw_id, "ok": False, "errors": ["Switch not found"]})
            continue
        if payload.system:
            try:
                await set_system_config(sw_id, payload.system)
            except Exception as e:
                sw_result["ok"] = False
                sw_result["errors"].append(f"system: {e}")
        if payload.poe:
            try:
                await set_poe_config(sw_id, payload.poe)
            except Exception as e:
                sw_result["ok"] = False
                sw_result["errors"].append(f"poe: {e}")
        if payload.ports:
            try:
                await set_ports_config(sw_id, payload.ports)
            except Exception as e:
                sw_result["ok"] = False
                sw_result["errors"].append(f"ports: {e}")
        if payload.ntp:
            try:
                await set_ntp_config(sw_id, payload.ntp)
            except Exception as e:
                sw_result["ok"] = False
                sw_result["errors"].append(f"ntp: {e}")
        if payload.loop:
            try:
                await set_loop_config(sw_id, payload.loop)
            except Exception as e:
                sw_result["ok"] = False
                sw_result["errors"].append(f"loop: {e}")
        if payload.ports_desc:
            try:
                await set_ports_desc(sw_id, payload.ports_desc)
            except Exception as e:
                sw_result["ok"] = False
                sw_result["errors"].append(f"ports_desc: {e}")
        if payload.snmp:
            try:
                await set_snmp_config(sw_id, payload.snmp)
            except Exception as e:
                sw_result["ok"] = False
                sw_result["errors"].append(f"snmp: {e}")
        results.append(sw_result)
    return {"results": results}


# ---------------------------------------------------------------------------
# SNMP / LLDP Topology
# ---------------------------------------------------------------------------

LLDP_REM_SYS_NAME  = "1.0.8802.1.1.2.1.4.1.1.9"
LLDP_REM_PORT_ID   = "1.0.8802.1.1.2.1.4.1.1.7"
LLDP_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"


async def _snmp_walk(host: str, community: str, base_oid: str) -> list[tuple[str, str]]:
    """SNMP v2c walk. Supports pysnmp v6 (async) and v4 (sync via thread)."""
    # --- pysnmp v6 lextudio (async API) ---
    try:
        from pysnmp.hlapi.v3arch.asyncio import (  # type: ignore
            SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
            ObjectType, ObjectIdentity, nextCmd,
        )
        engine = SnmpEngine()
        transport = await UdpTransportTarget.create((host, 161), timeout=3, retries=1)
        results: list[tuple[str, str]] = []
        async for err_ind, err_status, _, var_binds in nextCmd(
            engine,
            CommunityData(community, mpModel=1),
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if err_ind or err_status:
                break
            for oid, val in var_binds:
                results.append((str(oid), val.prettyPrint()))
        return results
    except (ImportError, AttributeError):
        pass

    # --- pysnmp v4 (synchronous, run in thread) ---
    def _sync_walk() -> list[tuple[str, str]]:
        from pysnmp.hlapi import (  # type: ignore
            SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
            ObjectType, ObjectIdentity, nextCmd,
        )
        res: list[tuple[str, str]] = []
        for err_ind, err_status, _, var_binds in nextCmd(
            SnmpEngine(),
            CommunityData(community, mpModel=1),
            UdpTransportTarget((host, 161), timeout=3, retries=1),
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
            lexicographicMode=False,
        ):
            if err_ind or err_status:
                break
            for oid, val in var_binds:
                res.append((str(oid), val.prettyPrint()))
        return res

    try:
        return await asyncio.to_thread(_sync_walk)
    except Exception:
        return []


def _lldp_index(oid_str: str, base_oid: str) -> tuple[int, int, int]:
    """Extract (timeMark, localPort, remIdx) from a LLDP OID string."""
    suffix = oid_str[len(base_oid):].lstrip(".")
    parts = suffix.split(".")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return 0, 0, 0


async def _get_lldp_neighbors(sw: dict) -> list[dict]:
    """Return LLDP neighbor list for one switch via SNMP."""
    try:
        raw = await _fetch(sw, "config/snmp")
        snmp_conf = parse_snmp(raw)
    except Exception:
        snmp_conf = {"enabled": False, "community_ro": "public"}

    if not snmp_conf.get("enabled"):
        return []

    community = snmp_conf.get("community_ro", "public") or "public"
    host = sw["ip"]

    sys_names_res, port_ids_res, port_desc_res = await asyncio.gather(
        _snmp_walk(host, community, LLDP_REM_SYS_NAME),
        _snmp_walk(host, community, LLDP_REM_PORT_ID),
        _snmp_walk(host, community, LLDP_REM_PORT_DESC),
        return_exceptions=True,
    )

    sys_name_map:  dict[tuple, str] = {}
    port_id_map:   dict[tuple, str] = {}
    port_desc_map: dict[tuple, str] = {}

    if not isinstance(sys_names_res, Exception):
        for oid, val in sys_names_res:
            sys_name_map[_lldp_index(oid, LLDP_REM_SYS_NAME)] = val
    if not isinstance(port_ids_res, Exception):
        for oid, val in port_ids_res:
            port_id_map[_lldp_index(oid, LLDP_REM_PORT_ID)] = val
    if not isinstance(port_desc_res, Exception):
        for oid, val in port_desc_res:
            port_desc_map[_lldp_index(oid, LLDP_REM_PORT_DESC)] = val

    neighbors = []
    for key, sys_name in sys_name_map.items():
        if not sys_name or sys_name.strip().lower() in ("", "noname"):
            continue
        _, local_port, _ = key
        neighbors.append({
            "local_port": local_port,
            "remote_sys_name": sys_name.strip(),
            "remote_port_id": port_id_map.get(key, ""),
            "remote_port_desc": port_desc_map.get(key, ""),
        })
    return neighbors


@app.get("/api/topology")
async def get_topology():
    """Build network topology graph using LLDP data collected via SNMP."""
    switches = load_switches()
    nodes = [
        {"id": sw["id"], "name": sw["name"], "ip": sw["ip"], "managed": True}
        for sw in switches
    ]
    name_lower_to_id = {sw["name"].lower(): sw["id"] for sw in switches}

    # Query all switches in parallel
    all_neighbors = await asyncio.gather(
        *[_get_lldp_neighbors(sw) for sw in switches],
        return_exceptions=True,
    )

    extra_nodes: dict[str, dict] = {}
    edges: list[dict] = []
    seen_edge_keys: set[tuple] = set()
    snmp_status: dict[str, str] = {}

    for sw, neighbors in zip(switches, all_neighbors):
        if isinstance(neighbors, Exception):
            snmp_status[sw["id"]] = f"error: {neighbors}"
            continue
        if not neighbors:
            snmp_status[sw["id"]] = "no_lldp"
            continue
        snmp_status[sw["id"]] = "ok"
        for nb in neighbors:
            remote_name = nb["remote_sys_name"]
            remote_id = name_lower_to_id.get(remote_name.lower())
            if not remote_id:
                if remote_name not in extra_nodes:
                    extra_nodes[remote_name] = {
                        "id": f"ext:{remote_name}",
                        "name": remote_name,
                        "ip": "",
                        "managed": False,
                    }
                remote_id = f"ext:{remote_name}"

            edge_key = tuple(sorted([sw["id"], remote_id]))
            if edge_key not in seen_edge_keys:
                seen_edge_keys.add(edge_key)
                edges.append({
                    "source": sw["id"],
                    "target": remote_id,
                    "source_port": nb["local_port"],
                    "target_port": nb["remote_port_desc"] or nb["remote_port_id"],
                })

    return {
        "nodes": nodes + list(extra_nodes.values()),
        "edges": edges,
        "snmp_status": snmp_status,
    }


# ---------------------------------------------------------------------------
# Serve the frontend
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
