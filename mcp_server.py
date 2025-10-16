#!/usr/bin/env python3
from fastmcp import FastMCP
from netmiko import ConnectHandler
import os, re, ipaddress
from typing import List, Dict, Any, Optional

mcp = FastMCP("Cisco Simple Interface MCP")

# --------------- CONNECTION ---------------
def get_connection():
    conn = ConnectHandler(
        device_type="cisco_ios",
        host=os.environ["CISCO_HOST"],
        username=os.environ["CISCO_USERNAME"],
        password=os.environ["CISCO_PASSWORD"],
        port=int(os.getenv("CISCO_PORT", "22")),
        conn_timeout=8,
        fast_cli=True,
    )
    secret = os.getenv("CISCO_SECRET")
    if secret:
        try:
            conn.enable()
        except Exception:
            conn.secret = secret
            conn.enable()
    return conn

def send_config(conn, commands: List[str]) -> str:
    return conn.send_config_set(commands)

# --------------- PARSERS ---------------
def parse_show_ip_int_brief(text: str):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    data = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        iface, ip, ok, method, status, protocol = parts[:6]
        data.append({
            "interface": iface,
            "ip": ip,
            "ok": ok,
            "method": method,
            "status": status,
            "protocol": protocol
        })
    return data

SHOW_VER_HOST_RE = re.compile(r"(?i)^(.+?) uptime is (.+)$")
SHOW_VER_VER_RE  = re.compile(r"(?i)Cisco IOS XE Software, Version ([^,\n]+)|Cisco IOS Software, .+ Version ([^,\n]+)")

def parse_show_version(raw: str) -> Dict[str, Any]:
    hostname = None
    uptime = None
    version = None
    for line in raw.splitlines():
        m = SHOW_VER_HOST_RE.search(line.strip())
        if m:
            hostname = m.group(1).strip()
            uptime = m.group(2).strip()
            break
    mv = SHOW_VER_VER_RE.search(raw)
    if mv:
        version = (mv.group(1) or mv.group(2) or "").strip()
    return {"hostname": hostname, "version": version, "uptime": uptime}

# --------------- IP HELPERS ---------------
def _norm_ip_and_mask(ip: str, mask: str) -> (str, str):
    ip = ip.strip()
    mask = (mask or "").strip()
    if "/" in ip:
        iface = ipaddress.ip_interface(ip)
        return str(iface.ip), str(iface.netmask)
    if mask:
        if mask.isdigit():
            m = ipaddress.ip_network(f"0.0.0.0/{mask}", strict=False).netmask
            return ip, str(m)
        else:
            _ = ipaddress.ip_network(f"0.0.0.0/{mask}", strict=False)
            return ip, mask
    raise ValueError("Mask required when IP is not CIDR.")

def _validate_interface_name(name: str) -> str:
    n = name.strip()
    if not n:
        raise ValueError("Interface name cannot be empty.")
    if any(ch in n for ch in "\n\r\t"):
        raise ValueError("Invalid characters in interface name.")
    return n

def _validate_loopback_id(n: int) -> int:
    n = int(n)
    if n < 0:
        raise ValueError("Loopback ID must be non-negative.")
    return n

# --------------- TOOLS ---------------
@mcp.tool(name="get_interfaces", description="Run 'show ip interface brief' and return raw + parsed output.")
def get_interfaces() -> dict:
    try:
        conn = get_connection()
        raw = conn.send_command("show ip interface brief")
        conn.disconnect()
        return {"raw": raw, "parsed": parse_show_ip_int_brief(raw)}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool(name="get_version", description="Run 'show version' and return raw + parsed.")
def get_version() -> dict:
    try:
        conn = get_connection()
        raw = conn.send_command("show version")
        conn.disconnect()
        return {"raw": raw, "parsed": parse_show_version(raw)}
    except Exception as e:
        return {"error": str(e)}

@mcp.tool(name="set_interface_ip", description="Set or replace IP on an interface. Return raw + parsed verification.")
def set_interface_ip(interface: str, ip: str, mask: Optional[str] = None,
                     replace: bool = True, no_shutdown: bool = True,
                     save: bool = False, dry_run: bool = False) -> dict:
    try:
        iface = _validate_interface_name(interface)
        addr, netmask = _norm_ip_and_mask(ip, mask)
        cmds = [f"interface {iface}"]
        if replace:
            cmds.append("no ip address")
        cmds.append(f"ip address {addr} {netmask}")
        if no_shutdown:
            cmds.append("no shutdown")

        if dry_run:
            return {"commands": cmds, "raw": None, "parsed": None, "saved": False, "dry_run": True}

        conn = get_connection()
        raw = send_config(conn, cmds)
        if save:
            raw += "\n" + conn.send_command("write memory")
        verify_raw = conn.send_command("show ip interface brief")
        conn.disconnect()
        return {
            "commands": cmds,
            "raw": raw,
            "parsed": parse_show_ip_int_brief(verify_raw),
            "saved": save
        }
    except Exception as e:
        return {"error": str(e)}

@mcp.tool(name="create_loopback", description="Create a loopback interface with IP. Return raw + parsed verification.")
def create_loopback(loopback_id: int, ip: str, mask: Optional[str] = None,
                    description: Optional[str] = None, save: bool = False,
                    dry_run: bool = False) -> dict:
    try:
        n = _validate_loopback_id(loopback_id)
        addr, netmask = _norm_ip_and_mask(ip, mask)
        iface = f"Loopback{n}"
        cmds = [f"interface {iface}"]
        if description:
            desc = " ".join(description.splitlines()).strip()
            cmds.append(f"description {desc}")
        cmds.append(f"ip address {addr} {netmask}")

        if dry_run:
            return {"commands": cmds, "raw": None, "parsed": None, "saved": False, "dry_run": True}

        conn = get_connection()
        raw = send_config(conn, cmds)
        if save:
            raw += "\n" + conn.send_command("write memory")
        verify_raw = conn.send_command("show ip interface brief")
        conn.disconnect()
        return {
            "commands": cmds,
            "raw": raw,
            "parsed": parse_show_ip_int_brief(verify_raw),
            "saved": save
        }
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    mcp.run()

