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

SWITCHES_FILE   = Path(__file__).parent / "switches.json"
SNMP_CFG_FILE   = Path(__file__).parent / "snmp_config.json"


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


_SNMP_CFG_DEFAULTS: dict = {"enabled": False, "seed_ips": [], "community": "public"}

def load_snmp_config() -> dict:
    if SNMP_CFG_FILE.exists():
        try:
            raw = json.loads(SNMP_CFG_FILE.read_text())
            # Migrate legacy single 'ip' field to seed_ips list
            if raw.get("ip") and not raw.get("seed_ips"):
                raw["seed_ips"] = [raw["ip"]]
            return {**_SNMP_CFG_DEFAULTS, **raw}
        except Exception:
            pass
    return dict(_SNMP_CFG_DEFAULTS)

def save_snmp_config(data: dict) -> None:
    SNMP_CFG_FILE.write_text(json.dumps(data, indent=2))


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


class SnmpDiscoveryConfig(BaseModel):
    enabled: bool
    seed_ips: list[str] = []
    community: str = "public"


# Legacy alias kept for any direct references
CoreSwitchConfig = SnmpDiscoveryConfig


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
            return {"ok": True}
    raise HTTPException(status_code=404, detail="Switch not found")


@app.delete("/api/switches/{switch_id}")
async def delete_switch(switch_id: str):
    switches = load_switches()
    new_list = [s for s in switches if s["id"] != switch_id]
    if len(new_list) == len(switches):
        raise HTTPException(status_code=404, detail="Switch not found")
    save_switches(new_list)
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
    # One session for all three fetches
    async with _switch_session(sw) as client:
        try:
            r1 = await client.get("/config/ports")
            r2 = await client.get("/stat/ports")
            r3 = await client.get("/stat/poe_status")
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc))
    config_raw = r1.text if r1.status_code == 200 and not r1.text.startswith("<!DOCTYPE") else ""
    traffic_raw = r2.text if r2.status_code == 200 and not r2.text.startswith("<!DOCTYPE") else ""
    poe_raw = r3.text if r3.status_code == 200 and not r3.text.startswith("<!DOCTYPE") else ""
    config = parse_port_config(config_raw)
    traffic = parse_ports(traffic_raw)
    poe = parse_poe_status(poe_raw)
    return merge_port_data(config, traffic, poe)


@app.get("/api/switches/{switch_id}/config/all")
async def get_all_config(switch_id: str):
    """Fetch all config tab data in a single switch session."""
    sw = _find_switch(switch_id)
    paths = [
        "config/sysinfo",
        "config/poe_config",
        "config/ports",
        "stat/ports",
        "stat/poe_status",
        "config/ntp",
        "config/ports_desc",
        "config/loop_config",
        "config/vlan",
        "config/pvlan",
        "config/aggregation",
        "config/snmp",
    ]
    async with _switch_session(sw) as client:
        try:
            raws = {}
            for path in paths:
                resp = await client.get(f"/{path}")
                raws[path] = resp.text if resp.status_code == 200 and not resp.text.startswith("<!DOCTYPE") else ""
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc))
    return {
        "system":      parse_sysinfo(raws["config/sysinfo"]),
        "poe":         parse_poe_config(raws["config/poe_config"]),
        "ports":       merge_port_data(
                           parse_port_config(raws["config/ports"]),
                           parse_ports(raws["stat/ports"]),
                           parse_poe_status(raws["stat/poe_status"]),
                       ),
        "ntp":         parse_ntp(raws["config/ntp"]),
        "ports_desc":  parse_ports_desc(raws["config/ports_desc"]),
        "loop":        parse_loop_config(raws["config/loop_config"]),
        "vlan":        parse_vlan(raws["config/vlan"]),
        "pvlan":       parse_pvlan(raws["config/pvlan"]),
        "aggregation": parse_aggregation(raws["config/aggregation"]),
        "snmp":        parse_snmp(raws["config/snmp"]),
    }


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
            async with _switch_session(sw) as client:
                r1 = await client.get("/stat/sys_overview")
                r2 = await client.get("/stat/poe_status")
            raw = r1.text if r1.status_code == 200 and not r1.text.startswith("<!DOCTYPE") else ""
            poe_raw = r2.text if r2.status_code == 200 and not r2.text.startswith("<!DOCTYPE") else ""
            overview = parse_sys_overview(raw)
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
# SNMP walk engine
# ---------------------------------------------------------------------------

# Global lock: pysnmp SnmpEngine instances interfere when run concurrently in
# the same event loop. Serialize all SNMP walks to ensure reliable results.
_snmp_lock: asyncio.Lock | None = None


def _get_snmp_lock() -> asyncio.Lock:
    global _snmp_lock
    if _snmp_lock is None:
        _snmp_lock = asyncio.Lock()
    return _snmp_lock


async def _snmp_walk(host: str, community: str, base_oid: str) -> list[tuple[str, str]]:
    """SNMP v2c walk. Supports pysnmp v7 (walk_cmd), v6 (nextCmd), and v4 (sync)."""
    async with _get_snmp_lock():
        return await _snmp_walk_inner(host, community, base_oid)


async def _snmp_walk_inner(host: str, community: str, base_oid: str) -> list[tuple[str, str]]:
    """Internal SNMP walk (called while holding _snmp_lock)."""
    subtree_prefix = base_oid + "."

    # --- pysnmp v7 lextudio (snake_case walk_cmd) ---
    try:
        from pysnmp.hlapi.v3arch.asyncio import (  # type: ignore
            SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
            ObjectType, ObjectIdentity, walk_cmd,
        )
        engine = SnmpEngine()
        transport = await UdpTransportTarget.create((host, 161), timeout=3, retries=1)
        results: list[tuple[str, str]] = []
        async for err_ind, err_status, _, var_binds in walk_cmd(
            engine,
            CommunityData(community, mpModel=1),
            transport,
            ContextData(),
            ObjectType(ObjectIdentity(base_oid)),
        ):
            if err_ind or err_status:
                break
            for oid, val in var_binds:
                oid_str = str(oid)
                if not oid_str.startswith(subtree_prefix):
                    return results
                results.append((oid_str, val.prettyPrint()))
        return results
    except (ImportError, AttributeError):
        pass

    # --- pysnmp v6 lextudio (camelCase nextCmd) ---
    try:
        from pysnmp.hlapi.v3arch.asyncio import (  # type: ignore
            SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
            ObjectType, ObjectIdentity, nextCmd,
        )
        engine = SnmpEngine()
        transport = await UdpTransportTarget.create((host, 161), timeout=3, retries=1)
        results2: list[tuple[str, str]] = []
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
                results2.append((str(oid), val.prettyPrint()))
        return results2
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


# ---------------------------------------------------------------------------
# SNMP helper utilities
# ---------------------------------------------------------------------------

def _parse_snmp_mac(val: str) -> str | None:
    """Convert SNMP OctetString prettyPrint output to 'aa:bb:cc:dd:ee:ff' format."""
    v = val.strip()
    if v.startswith("0x") and len(v) == 14:
        return ":".join(v[2+i:4+i].lower() for i in range(0, 12, 2))
    parts = v.split(":")
    if len(parts) == 6:
        try:
            return ":".join(f"{int(p, 16):02x}" for p in parts)
        except ValueError:
            pass
    import re as _re
    m = _re.findall(r'\\x([0-9a-fA-F]{2})', v)
    if len(m) == 6:
        return ":".join(b.lower() for b in m)
    return None


def _mac_from_fdb_oid(oid_str: str, base_oid: str) -> str | None:
    """Extract MAC from FDB OID suffix (6 decimal octets appended to base_oid)."""
    suffix = oid_str[len(base_oid):].lstrip(".")
    parts = suffix.split(".")
    if len(parts) < 6:
        return None
    try:
        return ":".join(f"{int(p):02x}" for p in parts[-6:])
    except ValueError:
        return None


def _lldp_key(oid_str: str, base_oid: str) -> tuple[int, int, int]:
    """Extract (timeMark, localPortNum, remIndex) from a LLDP OID string."""
    suffix = oid_str[len(base_oid):].lstrip(".")
    parts = suffix.split(".")
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except (IndexError, ValueError):
        return 0, 0, 0


# ---------------------------------------------------------------------------
# SNMP LLDP / FDB / ARP helpers
# ---------------------------------------------------------------------------

_LLDP_REM_CHASSIS_SUBTYPE = "1.0.8802.1.1.2.1.4.1.1.4"
_LLDP_REM_CHASSIS_ID      = "1.0.8802.1.1.2.1.4.1.1.5"
_LLDP_REM_PORT_ID         = "1.0.8802.1.1.2.1.4.1.1.7"
_LLDP_REM_PORT_DESC       = "1.0.8802.1.1.2.1.4.1.1.8"
_LLDP_REM_SYS_NAME        = "1.0.8802.1.1.2.1.4.1.1.9"
_LLDP_LOC_PORT_DESC       = "1.0.8802.1.1.2.1.3.7.1.4"
_LLDP_REM_MGMT_ADDR       = "1.0.8802.1.1.2.1.4.2.1.3"
_IF_DESCR_OID             = "1.3.6.1.2.1.2.2.1.2"
_SYS_NAME_OID             = "1.3.6.1.2.1.1.5"
_SYS_DESCR_OID            = "1.3.6.1.2.1.1.1"
_ARP_OID                  = "1.3.6.1.2.1.4.22.1.2"
_FDB_PORT_OID             = "1.3.6.1.2.1.17.4.3.1.2"
_FDB_STATUS_OID           = "1.3.6.1.2.1.17.4.3.1.3"
_BRIDGE_PORT_IFIDX_OID    = "1.3.6.1.2.1.17.1.4.1.2"


async def _snmp_get_ifnames(ip: str, community: str) -> dict[int, str]:
    """Walk ifDescr -> {ifIndex: ifName}."""
    rows = await _snmp_walk(ip, community, _IF_DESCR_OID)
    result: dict[int, str] = {}
    for oid, val in rows:
        idx = oid.rsplit(".", 1)[-1]
        if idx.isdigit():
            result[int(idx)] = val
    return result


async def _snmp_get_arp(ip: str, community: str) -> dict[str, str]:
    """Walk ARP table -> {mac_str: ip_str}."""
    rows = await _snmp_walk(ip, community, _ARP_OID)
    result: dict[str, str] = {}
    for oid, val in rows:
        suffix = oid[len(_ARP_OID):].lstrip(".")
        parts = suffix.split(".")
        if len(parts) >= 5:
            nbr_ip = ".".join(parts[-4:])
            mac = _parse_snmp_mac(val)
            if mac and nbr_ip:
                result[mac] = nbr_ip
    return result


async def _snmp_get_lldp_neighbors(ip: str, community: str) -> list[dict]:
    """
    Walk LLDP remote neighbor tables. Returns list of dicts:
    {lldp_key, local_port_num, chassis_subtype, chassis_id,
     remote_port_id, remote_port_desc, remote_sys_name}
    """
    cols = {
        "chassis_subtype": _LLDP_REM_CHASSIS_SUBTYPE,
        "chassis_id":      _LLDP_REM_CHASSIS_ID,
        "port_id":         _LLDP_REM_PORT_ID,
        "port_desc":       _LLDP_REM_PORT_DESC,
        "sys_name":        _LLDP_REM_SYS_NAME,
    }
    data: dict[tuple, dict] = {}
    for col, base in cols.items():
        rows = await _snmp_walk(ip, community, base)
        for oid, val in rows:
            key = _lldp_key(oid, base)
            if key == (0, 0, 0):
                continue
            data.setdefault(key, {})[col] = val

    neighbors = []
    for key, vals in data.items():
        if not vals.get("sys_name", "").strip():
            continue
        _, local_port_num, _ = key
        sub_raw = vals.get("chassis_subtype", "0")
        neighbors.append({
            "lldp_key":         key,
            "local_port_num":   local_port_num,
            "chassis_subtype":  int(sub_raw) if sub_raw.isdigit() else 0,
            "chassis_id":       vals.get("chassis_id", ""),
            "remote_port_id":   vals.get("port_id", ""),
            "remote_port_desc": vals.get("port_desc", ""),
            "remote_sys_name":  vals.get("sys_name", "").strip(),
        })
    return neighbors


async def _snmp_get_lldp_mgmt_ips(ip: str, community: str) -> dict[tuple, str]:
    """
    Walk lldpRemManAddrTable -> {(timeMark, localPort, remIdx): mgmt_ip_str}.
    IPv4 address is encoded in the OID suffix after addrSubtype=1.
    """
    rows = await _snmp_walk(ip, community, _LLDP_REM_MGMT_ADDR)
    result: dict[tuple, str] = {}
    for oid, _val in rows:
        suffix = oid[len(_LLDP_REM_MGMT_ADDR):].lstrip(".")
        parts = suffix.split(".")
        if len(parts) < 8:
            continue
        try:
            time_mark  = int(parts[0])
            local_port = int(parts[1])
            rem_idx    = int(parts[2])
            addr_sub   = int(parts[3])
            if addr_sub == 1 and len(parts) >= 8:   # IPv4
                mgmt_ip = ".".join(parts[4:8])
                key = (time_mark, local_port, rem_idx)
                if key not in result:
                    result[key] = mgmt_ip
        except (ValueError, IndexError):
            pass
    return result


async def _snmp_get_lldp_local_port_names(ip: str, community: str) -> dict[int, str]:
    """Walk lldpLocPortDesc -> {lldpPortNum: portDescription}."""
    rows = await _snmp_walk(ip, community, _LLDP_LOC_PORT_DESC)
    result: dict[int, str] = {}
    for oid, val in rows:
        suffix = oid[len(_LLDP_LOC_PORT_DESC):].lstrip(".")
        if suffix.isdigit():
            result[int(suffix)] = val
    return result


async def _snmp_get_fdb(
    ip: str, community: str, if_map: dict[int, str]
) -> dict[str, list[str]]:
    """
    Walk FDB -> {port_name: [mac_str, ...]} for learned entries only (status=3).
    """
    bp_rows, fp_rows, fs_rows = await asyncio.gather(
        _snmp_walk(ip, community, _BRIDGE_PORT_IFIDX_OID),
        _snmp_walk(ip, community, _FDB_PORT_OID),
        _snmp_walk(ip, community, _FDB_STATUS_OID),
        return_exceptions=True,
    )

    bp_to_if: dict[int, int] = {}
    if not isinstance(bp_rows, Exception):
        for oid, val in bp_rows:
            p = oid.rsplit(".", 1)[-1]
            if p.isdigit() and val.isdigit():
                bp_to_if[int(p)] = int(val)

    fdb_status: dict[str, int] = {}
    if not isinstance(fs_rows, Exception):
        for oid, val in fs_rows:
            mac = _mac_from_fdb_oid(oid, _FDB_STATUS_OID)
            if mac:
                try:
                    fdb_status[mac] = int(val)
                except ValueError:
                    pass

    result: dict[str, list[str]] = {}
    if not isinstance(fp_rows, Exception):
        for oid, val in fp_rows:
            mac = _mac_from_fdb_oid(oid, _FDB_PORT_OID)
            if not mac or not val.isdigit():
                continue
            # Skip non-learned entries only when status data is available.
            # If the status walk failed (fdb_status empty), include everything.
            if fdb_status and fdb_status.get(mac, 0) != 3:
                continue
            bp = int(val)
            if_idx = bp_to_if.get(bp)
            port_name = if_map.get(if_idx, f"port{bp}") if if_idx else f"bp{bp}"
            result.setdefault(port_name, []).append(mac)
    return result


# ---------------------------------------------------------------------------
# SNMP network auto-discovery
# ---------------------------------------------------------------------------

async def _discover_one_switch(ip: str, community: str) -> dict | None:
    """
    Query a single switch via SNMP: sysInfo, LLDP neighbors, ifNames, ARP, FDB.
    Returns None if SNMP is unreachable.
    """
    sys_name_rows, sys_descr_rows = await asyncio.gather(
        _snmp_walk(ip, community, _SYS_NAME_OID),
        _snmp_walk(ip, community, _SYS_DESCR_OID),
        return_exceptions=True,
    )
    sys_name_rows  = sys_name_rows  if not isinstance(sys_name_rows, Exception)  else []
    sys_descr_rows = sys_descr_rows if not isinstance(sys_descr_rows, Exception) else []

    if not sys_name_rows and not sys_descr_rows:
        return None   # SNMP not reachable

    sys_name  = sys_name_rows[0][1]  if sys_name_rows  else ip
    sys_descr = sys_descr_rows[0][1] if sys_descr_rows else ""

    lldp_neighbors, lldp_mgmt_ips, lldp_loc_ports, if_map, arp_map = await asyncio.gather(
        _snmp_get_lldp_neighbors(ip, community),
        _snmp_get_lldp_mgmt_ips(ip, community),
        _snmp_get_lldp_local_port_names(ip, community),
        _snmp_get_ifnames(ip, community),
        _snmp_get_arp(ip, community),
        return_exceptions=True,
    )
    lldp_neighbors = lldp_neighbors if not isinstance(lldp_neighbors, Exception) else []
    lldp_mgmt_ips  = lldp_mgmt_ips  if not isinstance(lldp_mgmt_ips,  Exception) else {}
    lldp_loc_ports = lldp_loc_ports if not isinstance(lldp_loc_ports, Exception) else {}
    if_map         = if_map         if not isinstance(if_map,         Exception) else {}
    arp_map        = arp_map        if not isinstance(arp_map,        Exception) else {}

    for n in lldp_neighbors:
        port_num = n["local_port_num"]
        n["local_port_name"] = (
            lldp_loc_ports.get(port_num)
            or if_map.get(port_num)
            or f"port{port_num}"
        )
        # Resolve remote IP: lldpRemManAddr > ARP via chassis MAC
        mgmt_ip = lldp_mgmt_ips.get(n["lldp_key"], "")
        if not mgmt_ip:
            chassis_mac = _parse_snmp_mac(n["chassis_id"])
            if chassis_mac:
                mgmt_ip = arp_map.get(chassis_mac, "")
        n["remote_ip"]       = mgmt_ip
        n["chassis_mac"]     = _parse_snmp_mac(n["chassis_id"]) or ""

    fdb = await _snmp_get_fdb(ip, community, if_map)

    # Build port_devices: port_name -> [{mac, ip}]
    port_devices: dict[str, list[dict]] = {}
    for port_name, macs in fdb.items():
        port_devices[port_name] = [
            {"mac": mac, "ip": arp_map.get(mac, "")} for mac in macs
        ]

    return {
        "ip":             ip,
        "sys_name":       sys_name,
        "sys_descr":      sys_descr,
        "lldp_neighbors": lldp_neighbors,
        "port_devices":   port_devices,
        "if_map":         if_map,
    }


async def _discover_network(seed_ips: list[str], community: str) -> dict[str, dict]:
    """
    Recursively discover all SNMP-reachable switches starting from seed_ips
    by following LLDP neighbor links. Returns {ip: switch_data_dict}.
    """
    discovered: dict[str, dict] = {}
    queue = list(dict.fromkeys(seed_ips))   # deduplicated, ordered
    in_queue: set[str] = set(queue)

    while queue:
        ip = queue.pop(0)
        if ip in discovered:
            continue
        sw_data = await _discover_one_switch(ip, community)
        if not sw_data:
            continue
        discovered[ip] = sw_data

        for n in sw_data.get("lldp_neighbors", []):
            n_ip = n.get("remote_ip", "")
            if not n_ip or n_ip in discovered or n_ip in in_queue:
                continue
            try:
                addr = ipaddress.IPv4Address(n_ip)
                if not addr.is_multicast and not addr.is_loopback:
                    queue.append(n_ip)
                    in_queue.add(n_ip)
            except ValueError:
                pass

    return discovered


def _build_snmp_topology(discovered: dict[str, dict], seed_ips: list[str]) -> dict:
    """
    Build nodes + edges from SNMP-discovered data.
    BFS from seed_ips to determine parent-child switch relationships.
    FDB data provides device-level connections.
    """
    all_ips = set(discovered.keys())
    nodes: list[dict] = []
    edges: list[dict] = []
    seen_devices: set[str] = set()

    # BFS from seeds to determine tree structure
    parent_map: dict[str, str | None] = {}
    port_to_child: dict[str, dict[str, tuple[str, str]]] = {}  # ip -> {child_ip: (local_port, remote_port)}
    bfs_visited: set[str] = set()
    bfs_queue = [ip for ip in seed_ips if ip in all_ips]
    for ip in bfs_queue:
        parent_map[ip] = None
        bfs_visited.add(ip)

    while bfs_queue:
        ip = bfs_queue.pop(0)
        sw = discovered[ip]
        port_to_child.setdefault(ip, {})
        for n in sw.get("lldp_neighbors", []):
            r_ip = n.get("remote_ip", "")
            if not r_ip or r_ip not in all_ips or r_ip in bfs_visited:
                continue
            bfs_visited.add(r_ip)
            parent_map[r_ip] = ip
            local_port = n["local_port_name"]
            # Find remote's port name for this link
            remote_port = next(
                (rn["local_port_name"] for rn in discovered.get(r_ip, {}).get("lldp_neighbors", [])
                 if rn.get("remote_ip") == ip),
                "",
            )
            port_to_child[ip][r_ip] = (local_port, remote_port)
            bfs_queue.append(r_ip)

    # Any switch not reached from BFS seeds becomes an additional root
    for ip in all_ips:
        if ip not in parent_map:
            parent_map[ip] = None

    root_ips = {ip for ip, parent in parent_map.items() if parent is None}

    # Virtual upstream node
    nodes.append({
        "id": "__upstream__",
        "name": "Upstream / Internet",
        "ip": "",
        "managed": False,
        "core": False,
    })

    # Switch nodes + switch-to-upstream edges for roots
    for ip, sw in discovered.items():
        nodes.append({
            "id":      f"sw_{ip}",
            "name":    sw["sys_name"] or ip,
            "ip":      ip,
            "managed": True,
            "descr":   sw.get("sys_descr", ""),
        })
        if ip in root_ips:
            edges.append({
                "source": "__upstream__",
                "target": f"sw_{ip}",
                "source_port": "",
                "target_port": "",
            })

    # Switch-to-switch edges from BFS tree
    inter_sw_ports: dict[str, set[str]] = {ip: set() for ip in all_ips}
    for parent_ip, children in port_to_child.items():
        for child_ip, (local_port, remote_port) in children.items():
            inter_sw_ports.setdefault(parent_ip, set()).add(local_port)
            inter_sw_ports.setdefault(child_ip, set()).add(remote_port)
            edges.append({
                "source":      f"sw_{parent_ip}",
                "target":      f"sw_{child_ip}",
                "source_port": local_port,
                "target_port": remote_port,
            })

    # Device nodes from FDB (skip ports carrying inter-switch links)
    for ip, sw in discovered.items():
        sw_id = f"sw_{ip}"
        skip_ports = inter_sw_ports.get(ip, set())
        neighbor_chassis_macs = {
            n["chassis_mac"] for n in sw.get("lldp_neighbors", []) if n.get("chassis_mac")
        }

        for port_name, devs in sw.get("port_devices", {}).items():
            if port_name in skip_ports:
                continue
            for dev in devs:
                mac = dev["mac"]
                if mac in neighbor_chassis_macs:
                    continue
                dev_ip  = dev.get("ip", "")
                dev_id  = f"dev_{mac.replace(':', '')}"
                if dev_id in seen_devices:
                    continue
                seen_devices.add(dev_id)
                nodes.append({
                    "id":      dev_id,
                    "name":    dev_ip or mac,
                    "ip":      dev_ip,
                    "managed": False,
                    "device":  True,
                })
                edges.append({
                    "source":      sw_id,
                    "target":      dev_id,
                    "source_port": port_name,
                    "target_port": "",
                })

    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# SNMP discovery settings API
# ---------------------------------------------------------------------------

@app.get("/api/settings/core-switch")
async def get_core_switch_config():
    return load_snmp_config()


@app.put("/api/settings/core-switch")
async def update_core_switch_config(config: SnmpDiscoveryConfig):
    data = config.model_dump()
    # Also keep legacy 'ip' field pointing to first seed for backwards compat
    data["ip"] = config.seed_ips[0] if config.seed_ips else ""
    save_snmp_config(data)
    return {"ok": True}


@app.post("/api/settings/core-switch/test")
async def test_core_switch_snmp():
    cfg = load_snmp_config()
    seed_ips = cfg.get("seed_ips") or ([cfg["ip"]] if cfg.get("ip") else [])
    if not seed_ips:
        raise HTTPException(400, "No seed IPs configured")
    ip = seed_ips[0]
    community = cfg.get("community", "public")
    sys_name_res  = await _snmp_walk(ip, community, _SYS_NAME_OID)
    sys_descr_res = await _snmp_walk(ip, community, _SYS_DESCR_OID)
    if not sys_name_res and not sys_descr_res:
        raise HTTPException(502, f"No SNMP response from {ip}")
    return {
        "ok":       True,
        "sys_name":  sys_name_res[0][1]  if sys_name_res  else "",
        "sys_descr": sys_descr_res[0][1] if sys_descr_res else "",
    }


# ---------------------------------------------------------------------------
# Topology endpoint - SNMP + Axis HTTP hybrid auto-discovery
# ---------------------------------------------------------------------------

# Simple TTL cache: avoids running expensive SNMP + HTTP queries on every request.
_topology_cache: dict = {}
_TOPOLOGY_CACHE_TTL = 90   # seconds


@app.post("/api/topology/refresh")
async def refresh_topology():
    """Invalidate the topology cache so the next GET rebuilds it."""
    _topology_cache.clear()
    return {"ok": True}


@app.get("/api/topology")
async def get_topology():
    """
    Hybrid topology discovery:
    1. SNMP LLDP from configured seed IPs -> finds all SNMP/LLDP-capable switches
       and builds port-level connectivity via FDB + ARP.
    2. Axis HTTP API (config/topology) for switches in the inventory ->
       identifies Axis switches that don't support LLDP and enumerates their
       connected devices with proper names and port numbers.
    3. Falls back to pure Axis HTTP topology when SNMP is not configured.

    This works for any network: standard LLDP networks discover all switches
    automatically; Axis-specific networks use the HTTP API for Axis switches
    while SNMP provides the core switch port mapping.
    """
    import time as _time
    cached = _topology_cache.get("data")
    if cached and (_time.monotonic() - _topology_cache.get("ts", 0)) < _TOPOLOGY_CACHE_TTL:
        return cached

    snmp_cfg = load_snmp_config()
    seed_ips: list[str] = snmp_cfg.get("seed_ips") or (
        [snmp_cfg["ip"]] if snmp_cfg.get("ip") else []
    )
    axis_switches = load_switches()

    if snmp_cfg.get("enabled") and seed_ips:
        community = snmp_cfg.get("community", "public")

        # Run SNMP discovery first (sequential), then Axis HTTP queries in parallel.
        # Separating these avoids UDP packet loss in the SNMP FDB walk caused
        # by concurrent HTTP connections to 20+ Axis switches.
        try:
            discovered: dict[str, dict] = await _discover_network(seed_ips, community)
        except Exception:
            discovered = {}

        axis_raw_list = await asyncio.gather(
            *[_fetch_topology_raw(sw) for sw in axis_switches],
            return_exceptions=True,
        )

        if discovered:
            # Build SNMP base topology (LLDP-discovered switches + their FDB devices)
            base = _build_snmp_topology(discovered, seed_ips)
            nodes: list[dict] = base["nodes"]
            edges: list[dict] = base["edges"]

            # Axis HTTP data: ip -> {sw, raw_topology}
            axis_ip_to_info: dict[str, dict] = {}
            for sw, raw in zip(axis_switches, axis_raw_list):
                if not isinstance(raw, Exception) and raw is not None:
                    axis_ip_to_info[sw["ip"]] = {"sw": sw, "raw": raw}

            if axis_ip_to_info:
                # Build IP -> core-switch port map from seed switch FDB
                seed_ip = seed_ips[0]
                seed_data = discovered.get(seed_ip, {})
                ip_to_seed_port: dict[str, str] = {}
                for port_name, devs in seed_data.get("port_devices", {}).items():
                    for dev in devs:
                        if dev.get("ip"):
                            ip_to_seed_port[dev["ip"]] = port_name

                all_axis_ips = {sw["ip"] for sw in axis_switches}

                # Remove FDB device nodes that are actually Axis switches
                removed_dev_ids = {
                    n["id"] for n in nodes
                    if n.get("device") and n.get("ip") in all_axis_ips
                }
                nodes = [n for n in nodes if n["id"] not in removed_dev_ids]
                edges = [e for e in edges if e["target"] not in removed_dev_ids]

                existing_sw_ids = {n["id"] for n in nodes if n.get("managed")}
                seed_sw_id    = f"sw_{seed_ip}"
                parent_sw_id  = seed_sw_id if seed_sw_id in existing_sw_ids else "__upstream__"

                # Build HP port -> set of Axis IPs on that port (from SNMP FDB).
                # Ports with 2+ Axis switches indicate a daisy-chain pair.
                hp_port_axis_set: dict[str, set[str]] = {}
                for a_ip in all_axis_ips:
                    hp_port = ip_to_seed_port.get(a_ip)
                    if hp_port:
                        hp_port_axis_set.setdefault(hp_port, set()).add(a_ip)
                shared_hp_ports: dict[str, set[str]] = {
                    p: ips for p, ips in hp_port_axis_set.items() if len(ips) > 1
                }

                # Build Axis-to-Axis daisy-chain map from HTTP topology.
                # Collect ALL potential parent/child pairs where switch A reports
                # switch B as a direct neighbor (gw_mac == A's own_mac).
                # Then validate: only accept pairs where BOTH switches appear on
                # the same HP FDB port (shared_hp_ports). This prevents false
                # positives caused by management-gateway MAC reuse across VLANs.
                axis_child_candidates: dict[str, list[tuple[str, str]]] = {}
                for axis_ip, info in axis_ip_to_info.items():
                    own_mac = info["raw"].get("own_mac", "").lower()
                    if not own_mac:
                        continue
                    for nb in info["raw"].get("neighbors", []):
                        nb_ip = nb.get("ip", "")
                        if nb_ip not in all_axis_ips:
                            continue
                        gw_mac = nb.get("gw_mac", "").lower()
                        if gw_mac == own_mac:
                            axis_child_candidates.setdefault(nb_ip, []).append(
                                (axis_ip, nb.get("port", ""))
                            )

                # Validate candidates against shared HP FDB port groups.
                axis_child_map: dict[str, tuple[str, str]] = {}
                for child_ip, candidates in axis_child_candidates.items():
                    for parent_ip, parent_port in candidates:
                        for port_ips in shared_hp_ports.values():
                            if child_ip in port_ips and parent_ip in port_ips:
                                axis_child_map[child_ip] = (parent_ip, parent_port)
                                break
                        if child_ip in axis_child_map:
                            break

                # Break cycles: A is child of B AND B is child of A (both
                # switches report each other as direct neighbors).
                # Keep the direction with the HIGHER source port number
                # (expansion / inter-switch ports are numbered higher on T8508).
                # Tiebreaker: keep the mapping where the child has the higher IP.
                seen_pairs: set[frozenset] = set()
                to_delete: set[str] = set()
                for child_ip, (parent_ip, parent_port) in list(axis_child_map.items()):
                    if parent_ip not in axis_child_map:
                        continue
                    if axis_child_map[parent_ip][0] != child_ip:
                        continue
                    pair = frozenset([child_ip, parent_ip])
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    other_port = axis_child_map[parent_ip][1]
                    try:
                        my_n = int(parent_port)
                    except (ValueError, TypeError):
                        my_n = 0
                    try:
                        other_n = int(other_port)
                    except (ValueError, TypeError):
                        other_n = 0
                    if my_n > other_n:
                        to_delete.add(parent_ip)   # keep child_ip->parent_ip
                    elif my_n < other_n:
                        to_delete.add(child_ip)    # keep parent_ip->child_ip
                    else:
                        # Tie: keep higher IP as the child
                        if child_ip > parent_ip:
                            to_delete.add(parent_ip)
                        else:
                            to_delete.add(child_ip)
                for ip in to_delete:
                    del axis_child_map[ip]

                # Build device name and direct-placement lookups from Axis HTTP.
                device_name: dict[str, str] = {}
                # axis_direct_devices: device_ip -> (axis_ip, port_on_axis)
                axis_direct_devices: dict[str, tuple[str, str]] = {}

                for axis_ip, info in axis_ip_to_info.items():
                    own_mac = info["raw"].get("own_mac", "").lower()
                    for nb in info["raw"].get("neighbors", []):
                        nb_ip = nb.get("ip", "")
                        if not nb_ip or nb_ip in all_axis_ips:
                            continue
                        nm_val = nb.get("name") or nb.get("model") or ""
                        if nm_val and nb_ip not in device_name:
                            device_name[nb_ip] = nm_val
                        gw_mac = nb.get("gw_mac", "").lower()
                        if gw_mac == own_mac and nb_ip not in axis_direct_devices:
                            axis_direct_devices[nb_ip] = (axis_ip, nb.get("port", ""))

                # Add ALL Axis switches as managed nodes (even those with no HTTP data).
                # Daisy-chained switches get an edge from their parent Axis switch;
                # top-level switches get an edge from the HP seed switch.
                for sw in axis_switches:
                    axis_ip   = sw["ip"]
                    sw_node_id = f"sw_{axis_ip}"
                    if sw_node_id not in existing_sw_ids:
                        info = axis_ip_to_info.get(axis_ip)
                        nodes.append({
                            "id":      sw_node_id,
                            "name":    info["sw"]["name"] if info else sw["name"],
                            "ip":      axis_ip,
                            "managed": True,
                        })
                        existing_sw_ids.add(sw_node_id)

                    # Build the edge for this switch (deferred so all nodes exist first)

                # Now build inter-switch edges (after all nodes are added)
                for sw in axis_switches:
                    axis_ip    = sw["ip"]
                    sw_node_id = f"sw_{axis_ip}"
                    # Skip if an edge to this switch already exists
                    if any(e["target"] == sw_node_id for e in edges):
                        continue
                    if axis_ip in axis_child_map:
                        # Daisy-chained: parent is another Axis switch
                        parent_axis_ip, parent_port = axis_child_map[axis_ip]
                        edges.append({
                            "source":      f"sw_{parent_axis_ip}",
                            "target":      sw_node_id,
                            "source_port": parent_port,
                            "target_port": "",
                        })
                    else:
                        # Top-level: connect to HP seed switch
                        edges.append({
                            "source":      parent_sw_id,
                            "target":      sw_node_id,
                            "source_port": ip_to_seed_port.get(axis_ip, ""),
                            "target_port": "",
                        })

                # Reassign FDB device edges.
                # Priority 1 (Axis HTTP direct): if the device is known to be
                # directly connected to a specific Axis switch, use that.
                # Priority 2 (HP FDB port fallback): for devices whose IP is in
                # SNMP ARP but not in Axis HTTP topology, fall back to routing
                # via the HP port -> Axis switch map.
                # Build HP port -> all Axis switch IPs set for the fallback.
                # Build HP port -> top-level Axis switch (directly connected to HP)
                # and HP port -> full cluster (top-level + daisy-chain children).
                # HP FDB is authoritative for WHICH switch cluster a device is in.
                # axis_direct_devices enriches the port and picks the exact child
                # switch only when it agrees with the HP FDB cluster.
                axis_to_hp_port: dict[str, str] = {}
                for a_ip in all_axis_ips:
                    if a_ip in ip_to_seed_port:
                        axis_to_hp_port[a_ip] = ip_to_seed_port[a_ip]
                # Daisy-chain children share their parent's HP port
                for child_ip, (parent_ip, _) in axis_child_map.items():
                    if parent_ip in axis_to_hp_port and child_ip not in axis_to_hp_port:
                        axis_to_hp_port[child_ip] = axis_to_hp_port[parent_ip]
                hp_port_cluster: dict[str, set[str]] = {}
                for a_ip, port in axis_to_hp_port.items():
                    hp_port_cluster.setdefault(port, set()).add(a_ip)
                hp_port_top: dict[str, str] = {}
                for e in edges:
                    if e["source"] != seed_sw_id:
                        continue
                    tgt = next((n for n in nodes if n["id"] == e["target"]), None)
                    if tgt and tgt.get("managed") and tgt.get("ip") != seed_ip:
                        hp_port_top[e["source_port"]] = tgt["ip"]

                node_by_id = {n["id"]: n for n in nodes}
                for e in edges:
                    if e["source"] != seed_sw_id:
                        continue
                    tgt = node_by_id.get(e["target"])
                    if not tgt or not tgt.get("device"):
                        continue
                    dev_ip  = tgt.get("ip", "")
                    hp_port = e.get("source_port", "")
                    # Enrich name from Axis data
                    if dev_ip in device_name and (not tgt["name"] or tgt["name"] == dev_ip):
                        tgt["name"] = device_name[dev_ip]
                    if not hp_port:
                        continue
                    top_axis = hp_port_top.get(hp_port)
                    if not top_axis:
                        continue
                    cluster = hp_port_cluster.get(hp_port, {top_axis})
                    # Use axis_direct_devices only when it agrees with the HP cluster.
                    # This prevents false positives from management-gateway MACs.
                    direct = axis_direct_devices.get(dev_ip)
                    if direct and direct[0] in cluster:
                        e["source"]      = f"sw_{direct[0]}"
                        e["source_port"] = direct[1]
                    else:
                        e["source"]      = f"sw_{top_axis}"
                        e["source_port"] = ""

                # Remove IP-less FDB device nodes (created when SNMP ARP failed).
                # They have no useful information and can't be routed anywhere.
                no_ip_dev_ids = {
                    n["id"] for n in nodes
                    if n.get("device") and not n.get("ip")
                }
                if no_ip_dev_ids:
                    nodes = [n for n in nodes if n["id"] not in no_ip_dev_ids]
                    edges = [e for e in edges if e["target"] not in no_ip_dev_ids]

                # Add device nodes from Axis HTTP direct neighbors for any device
                # not already present. This fills the gap when SNMP ARP fails.
                existing_dev_ips = {n["ip"] for n in nodes if n.get("device") and n.get("ip")}
                for axis_ip, info in axis_ip_to_info.items():
                    own_mac    = info["raw"].get("own_mac", "").lower()
                    sw_node_id = f"sw_{axis_ip}"
                    for nb in info["raw"].get("neighbors", []):
                        nb_ip = nb.get("ip", "")
                        if not nb_ip or nb_ip in all_axis_ips or nb_ip in existing_dev_ips:
                            continue
                        gw_mac = nb.get("gw_mac", "").lower()
                        if gw_mac != own_mac:
                            continue  # only directly-connected devices
                        nb_mac  = (nb.get("mac") or "").replace("-", "").replace(":", "").lower()
                        dev_id  = f"dev_{nb_mac}" if nb_mac else f"dev_ip_{nb_ip.replace('.', '_')}"
                        nm_val  = nb.get("name") or nb.get("model") or nb_ip
                        nodes.append({
                            "id":      dev_id,
                            "name":    nm_val,
                            "ip":      nb_ip,
                            "managed": False,
                            "device":  True,
                        })
                        edges.append({
                            "source":      sw_node_id,
                            "target":      dev_id,
                            "source_port": nb.get("port", ""),
                            "target_port": "",
                        })
                        existing_dev_ips.add(nb_ip)

            sw_count  = sum(1 for n in nodes if n.get("managed"))
            dev_count = sum(1 for n in nodes if n.get("device"))
            result = {
                "nodes": nodes,
                "edges": edges,
                "snmp_status": {
                    "ok":             True,
                    "switches_found": sw_count,
                    "discovery":      "snmp+axis_http" if axis_ip_to_info else "snmp_lldp",
                },
            }
            _topology_cache["data"] = result
            _topology_cache["ts"]   = _time.monotonic()
            return result

    # -----------------------------------------------------------------------
    # Fallback: pure Axis HTTP API topology (SNMP not configured)
    # -----------------------------------------------------------------------
    switches = axis_switches
    nodes = [
        {"id": sw["id"], "name": sw["name"], "ip": sw["ip"], "managed": True}
        for sw in switches
    ]
    ip_to_sw = {sw["ip"]: sw for sw in switches}

    raw_results = await asyncio.gather(
        *[_fetch_topology_raw(sw) for sw in switches],
        return_exceptions=True,
    )

    sw_own_mac:   dict[str, str]         = {}
    sw_neighbors: dict[str, list[dict]]  = {}
    http_status:  dict[str, str]         = {}

    for sw, result in zip(switches, raw_results):
        if isinstance(result, Exception):
            http_status[sw["id"]] = f"error: {result}"
            continue
        if result is None:
            http_status[sw["id"]] = "no_data"
            continue
        http_status[sw["id"]] = "ok"
        own_mac = result["own_mac"]
        sw_own_mac[sw["id"]] = own_mac
        sw_neighbors[sw["id"]] = result["neighbors"]

    direct: dict[str, dict[str, str]] = {}
    for sw in switches:
        sw_id  = sw["id"]
        my_mac = sw_own_mac.get(sw_id, "")
        direct[sw_id] = {}
        neighbors = sw_neighbors.get(sw_id, [])

        port_counts: dict[str, int] = {}
        for nb in neighbors:
            if nb["ip"] in ip_to_sw:
                port_counts[nb["port"]] = port_counts.get(nb["port"], 0) + 1
        max_count  = max(port_counts.values(), default=0)
        uplink_ports = {p for p, c in port_counts.items() if c == max_count} if max_count > 1 else set()

        for nb in neighbors:
            nb_ip  = nb["ip"]
            gw_mac = nb["gw_mac"]
            nb_sw  = ip_to_sw.get(nb_ip)
            if not nb_sw or nb_sw["id"] == sw_id:
                continue
            if nb["port"] in uplink_ports:
                continue
            if gw_mac and my_mac and gw_mac == my_mac:
                nb_id = nb_sw["id"]
                if nb_id not in direct[sw_id]:
                    direct[sw_id][nb_id] = nb["port"]

    edges: list[dict] = []
    seen_edge_keys: set[tuple] = set()
    for sw_id, nbrs in direct.items():
        for nb_id, local_port in nbrs.items():
            edge_key = tuple(sorted([sw_id, nb_id]))
            if edge_key in seen_edge_keys:
                continue
            seen_edge_keys.add(edge_key)
            edges.append({
                "source":      sw_id,
                "target":      nb_id,
                "source_port": local_port,
                "target_port": direct.get(nb_id, {}).get(sw_id, ""),
            })

    adjacency: dict[str, set[str]] = {sw["id"]: set() for sw in switches}
    for sw_id, nbrs in direct.items():
        for nb_id in nbrs:
            adjacency[sw_id].add(nb_id)
            adjacency[nb_id].add(sw_id)

    visited: set[str] = set()
    components: list[set[str]] = []
    for sw in switches:
        sid = sw["id"]
        if sid in visited:
            continue
        component: set[str] = set()
        bq = [sid]
        while bq:
            node = bq.pop(0)
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            for nb in adjacency[node]:
                if nb not in visited:
                    bq.append(nb)
        components.append(component)

    UPSTREAM_ID = "__upstream__"
    nodes.append({"id": UPSTREAM_ID, "name": "Upstream Network", "ip": "", "managed": False})
    for component in components:
        root    = max(component, key=lambda sid: len(adjacency.get(sid, set())))
        root_sw = next((sw for sw in switches if sw["id"] == root), None)
        edges.append({
            "source": UPSTREAM_ID, "target": root, "source_port": "", "target_port": "",
        })

    seen_device_ips: set[str] = set()
    for sw in switches:
        sw_id  = sw["id"]
        my_mac = sw_own_mac.get(sw_id, "")
        if not my_mac:
            continue
        neighbors = sw_neighbors.get(sw_id, [])
        port_counts_l: dict[str, int] = {}
        for nb in neighbors:
            if nb["ip"] in ip_to_sw:
                port_counts_l[nb["port"]] = port_counts_l.get(nb["port"], 0) + 1
        max_c = max(port_counts_l.values(), default=0)
        uplink_l = {p for p, c in port_counts_l.items() if c == max_c} if max_c > 1 else set()

        for nb in neighbors:
            ip   = nb["ip"]
            port = nb["port"]
            if ip in ip_to_sw or port in uplink_l or nb["gw_mac"] != my_mac:
                continue
            if ip in seen_device_ips:
                continue
            seen_device_ips.add(ip)
            dev_id = f"dev_{ip}"
            nodes.append({"id": dev_id, "name": nb["name"] or nb["model"] or ip,
                          "ip": ip, "managed": False, "device": True})
            edges.append({"source": sw_id, "target": dev_id,
                          "source_port": port, "target_port": ""})

        for nb in neighbors:
            ip   = nb["ip"]
            port = nb["port"]
            if ip in ip_to_sw or port not in uplink_l or nb["gw_mac"] != my_mac:
                continue
            if ip in seen_device_ips:
                continue
            seen_device_ips.add(ip)
            dev_id = f"dev_{ip}"
            nodes.append({"id": dev_id, "name": nb["name"] or nb["model"] or ip,
                          "ip": ip, "managed": False, "device": True})
            edges.append({"source": UPSTREAM_ID, "target": dev_id,
                          "source_port": "", "target_port": ""})

    result = {"nodes": nodes, "edges": edges, "snmp_status": http_status}
    _topology_cache["data"] = result
    _topology_cache["ts"]   = _time.monotonic()
    return result


async def _fetch_topology_raw(sw: dict) -> dict | None:
    """
    Fetch config/topology from a switch.
    Returns {'own_mac': str, 'neighbors': [dict, ...]}
    Each neighbor dict: {ip, port, gw_mac, name, model, mac}

    The Axis topology response is one continuous stream where device records
    are delimited by '|/' (pipe-slash). Embedded newlines are line-wrapping only.
    Fields within each record are '|'-separated:
      0=device_mac, 1=gw_mac, 2=vlan, 3=local_port, 4=status, 5=ip,
      6=(empty), 7=name (URL-encoded), 8=model (URL-encoded)
    The self-entry (first record) is prefixed with '^'.
    """
    from urllib.parse import unquote
    try:
        async with _switch_session(sw) as client:
            resp = await client.get("/config/topology")
            if resp.status_code != 200:
                return None
            text = resp.text
    except Exception:
        return None

    # Strip line-wrapping newlines; records are separated by |/
    text = text.replace("\r", "").replace("\n", "")
    if text.startswith("^"):
        text = text[1:]

    own_mac: str = ""
    neighbors: list[dict] = []

    for i, record in enumerate(text.split("|/")):
        parts = record.split("|")
        if len(parts) < 6:
            continue

        device_mac = parts[0]
        gw_mac     = parts[1] if len(parts) > 1 else ""
        local_port = parts[3] if len(parts) > 3 else ""
        ip         = parts[5] if len(parts) > 5 else ""
        name       = unquote(parts[7]) if len(parts) > 7 else ""
        model      = unquote(parts[8]) if len(parts) > 8 else ""

        if i == 0:
            own_mac = device_mac
            continue

        if not device_mac:
            continue

        neighbors.append({
            "mac":    device_mac,
            "gw_mac": gw_mac,
            "port":   local_port,
            "ip":     ip,
            "name":   name,
            "model":  model,
        })

    return {"own_mac": own_mac, "neighbors": neighbors}




# ---------------------------------------------------------------------------
# Serve the frontend
# ---------------------------------------------------------------------------

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
