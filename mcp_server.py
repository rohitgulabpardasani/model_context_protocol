#!/usr/bin/env python3
"""
Cisco Simple Interface MCP (YAML-backed inventory)

Requires:
  pip install fastmcp netmiko pyyaml

Usage:
  python server.py --inventory devices.yaml
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import re
from typing import List, Dict, Any, Optional

import yaml
from fastmcp import FastMCP
from netmiko import ConnectHandler

# -----------------------------------------
# MCP App
# -----------------------------------------
mcp = FastMCP("Cisco Simple Interface MCP (YAML Inventory)")

# -----------------------------------------
# Inventory Loading
# -----------------------------------------
DEVICES: Dict[str, Dict[str, Any]] = {}
INVENTORY_PATH: str = "devices.yaml"


def load_inventory(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Inventory file not found: {path}")
    with open(path, "r") as f:
        data = yaml.safe_load(f) or {}
    devices = data.get("devices") or {}
    if not isinstance(devices, dict) or not devices:
        raise ValueError("Inventory must contain a top-level 'devices' mapping with at least one device.")
    # Basic normalization
    for name, d in devices.items():
        if "host" not in d or "username" not in d or "password" not in d:
            raise ValueError(f"Device '{name}' must include 'host', 'username', and 'password'.")
        d.setdefault("port", 22)
        d.setdefault("device_type", "cisco_ios")
    return devices


def default_device_name() -> str:
    # First device in the inventory
    return next(iter(DEVICES))


# -----------------------------------------
# Connections & Helpers
# -----------------------------------------
def get_connection(device_name: Optional[str] = None):
    """
    Open a Netmiko connection using credentials from DEVICES.
    """
    if not DEVICES:
        raise RuntimeError("Device inventory not loaded.")
    name = device_name or default_device_name()
    if name not in DEVICES:
        raise ValueError(f"Unknown device: {name}")
    dev = DEVICES[name]

    conn = ConnectHandler(
        device_type=dev.get("device_type", "cisco_ios"),
        host=dev["host"],
        username=dev["username"],
        password=dev["password"],
        port=int(dev.get("port", 22)),
        conn_timeout=8,
        fast_cli=True,
    )

    secret = dev.get("secret")
    if secret:
        try:
            # try current secret if preconfigured in profile
            conn.enable()
        except Exception:
            # set secret explicitly then enable
            conn.secret = secret
            conn.enable()
    return conn


def send_config(conn, commands: List[str]) -> str:
    return conn.send_config_set(commands)


# -----------------------------------------
# Parsers
# -----------------------------------------
def parse_show_ip_int_brief(text: str):
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return []
    data = []
    # Cisco variants sometimes include a header; skip first line
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        iface, ip, ok, method, status, protocol = parts[:6]
        data.append(
            {
                "interface": iface,
                "ip": ip,
                "ok": ok,
                "method": method,
                "status": status,
                "protocol": protocol,
            }
        )
    return data


SHOW_VER_HOST_RE = re.compile(r"(?i)^(.+?) uptime is (.+)$")
SHOW_VER_VER_RE = re.compile(
    r"(?i)Cisco IOS XE Software, Version ([^,\n]+)|Cisco IOS Software, .+ Version ([^,\n]+)"
)


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


# -----------------------------------------
# IP & Input Validation Helpers
# -----------------------------------------
def _norm_ip_and_mask(ip: str, mask: Optional[str]) -> (str, str):
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
    raise ValueError("Mask required when IP is not CIDR (e.g., ip='10.0.0.1', mask='24' or '255.255.255.0').")


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


# -----------------------------------------
# MCP Tools
# -----------------------------------------
@mcp.tool(name="list_devices", description="List available device names from the YAML inventory.")
def list_devices() -> dict:
    try:
        return {"devices": list(DEVICES.keys())}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="get_interfaces",
    description="Run 'show ip interface brief' and return raw + parsed output for a device.",
)
def get_interfaces(device: Optional[str] = None) -> dict:
    try:
        conn = get_connection(device)
        raw = conn.send_command("show ip interface brief")
        conn.disconnect()
        return {"device": device or default_device_name(), "raw": raw, "parsed": parse_show_ip_int_brief(raw)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(name="get_version", description="Run 'show version' and return raw + parsed for a device.")
def get_version(device: Optional[str] = None) -> dict:
    try:
        conn = get_connection(device)
        raw = conn.send_command("show version")
        conn.disconnect()
        return {"device": device or default_device_name(), "raw": raw, "parsed": parse_show_version(raw)}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="set_interface_ip",
    description="Set/replace IP on an interface. Verifies with 'show ip interface brief'.",
)
def set_interface_ip(
    interface: str,
    ip: str,
    mask: Optional[str] = None,
    device: Optional[str] = None,
    replace: bool = True,
    no_shutdown: bool = True,
    save: bool = False,
    dry_run: bool = False,
) -> dict:
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
            return {
                "device": device or default_device_name(),
                "commands": cmds,
                "raw": None,
                "parsed": None,
                "saved": False,
                "dry_run": True,
            }

        conn = get_connection(device)
        raw = send_config(conn, cmds)
        if save:
            raw += "\n" + conn.send_command("write memory")
        verify_raw = conn.send_command("show ip interface brief")
        conn.disconnect()
        return {
            "device": device or default_device_name(),
            "commands": cmds,
            "raw": raw,
            "parsed": parse_show_ip_int_brief(verify_raw),
            "saved": save,
            "dry_run": False,
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool(
    name="create_loopback",
    description="Create a loopback interface with IP. Verifies with 'show ip interface brief'.",
)
def create_loopback(
    loopback_id: int,
    ip: str,
    mask: Optional[str] = None,
    device: Optional[str] = None,
    description: Optional[str] = None,
    save: bool = False,
    dry_run: bool = False,
) -> dict:
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
            return {
                "device": device or default_device_name(),
                "commands": cmds,
                "raw": None,
                "parsed": None,
                "saved": False,
                "dry_run": True,
            }

        conn = get_connection(device)
        raw = send_config(conn, cmds)
        if save:
            raw += "\n" + conn.send_command("write memory")
        verify_raw = conn.send_command("show ip interface brief")
        conn.disconnect()
        return {
            "device": device or default_device_name(),
            "commands": cmds,
            "raw": raw,
            "parsed": parse_show_ip_int_brief(verify_raw),
            "saved": save,
            "dry_run": False,
        }
    except Exception as e:
        return {"error": str(e)}


# -----------------------------------------
# Main
# -----------------------------------------
def _parse_args():
    p = argparse.ArgumentParser(description="Cisco MCP server using YAML inventory.")
    p.add_argument("--inventory", "-i", default="devices.yaml", help="Path to devices YAML (default: devices.yaml)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    INVENTORY_PATH = args.inventory
    DEVICES = load_inventory(INVENTORY_PATH)
    mcp.run()

