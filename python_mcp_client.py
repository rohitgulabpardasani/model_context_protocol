#!/usr/bin/env python3
import json, subprocess, threading, time, os, sys, re, ipaddress
from pathlib import Path

# ----------------------------------------------------
# Launch your YAML-inventory server (unchanged)
# ----------------------------------------------------
SERVER_CMD = ["python3", "mcp_server.py", "--inventory", "devices.yaml"]

# ====================================================
#                  MCP Wire Client
# ====================================================
class MCPClient:
    def __init__(self, cmd, env=None):
        env_final = os.environ.copy()
        if env:
            env_final.update(env)
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env_final,
        )
        self._qid = 0
        self.responses = {}
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            s = line.strip()
            if not s:
                continue
            try:
                msg = json.loads(s)
            except Exception:
                # surface warnings/errors printed by the server
                if "WARNING" in s or "ERROR" in s:
                    print(s)
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                self.responses[msg["id"]] = msg

    def _next_id(self):
        self._qid += 1
        return self._qid

    def send(self, method, params=None, *, msg_id=None):
        if msg_id is None:
            msg_id = self._next_id()
        obj = {"jsonrpc": "2.0", "id": msg_id, "method": method}
        if params is not None:
            obj["params"] = params
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()
        return msg_id

    def request(self, method, params=None, timeout=25):
        rid = self.send(method, params)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if rid in self.responses:
                return self.responses.pop(rid)
            time.sleep(0.02)
        raise TimeoutError(f"Timeout waiting for {method}")

    def notify(self, method, params=None):
        obj = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            obj["params"] = params
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass

# ====================================================
#               Tool Call / Result Helpers
# ====================================================
def _merge_content_blocks(call_result):
    """
    Merge all JSON content blocks (preferred), fallback to JSON in text blocks.
    Returns the dict exactly as the server sent it (no default keys injected).
    """
    result = call_result.get("result", {})
    blocks = result.get("content", [])
    merged = {}
    found_any = False

    for b in blocks:
        if b.get("type") == "json":
            data = b.get("data", {})
            if isinstance(data, dict):
                merged.update(data)
                found_any = True

    if not found_any:
        for b in blocks:
            if b.get("type") == "text":
                try:
                    j = json.loads(b["text"])
                    if isinstance(j, dict):
                        merged.update(j)
                        found_any = True
                except Exception:
                    pass

    return merged if found_any else {}

def call_tool_raw(client, tool_name, arguments=None, timeout=90):
    arguments = arguments or {}
    call = client.request("tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)
    if "error" in call:
        raise RuntimeError(f"Tool '{tool_name}' error: {call['error']}")
    return _merge_content_blocks(call)

def call_tool_norm(client, tool_name, arguments=None, timeout=90):
    """
    Normalized output for pretty printing (ensures keys exist) + version recovery.
    """
    data = call_tool_raw(client, tool_name, arguments, timeout)
    for k in ["raw", "parsed", "commands", "saved", "dry_run", "device", "error"]:
        data.setdefault(k, None)
    # If this is a get_version payload and parsed.version is missing, try to recover from raw
    if tool_name == "get_version":
        _recover_version_inplace(data)
    return data

# ---------------- Version Recovery (Client-side) ----------------
_VERSION_PATTERNS = [
    r"(?i)Cisco IOS XE Software,\s*Version\s+([^,\n]+)",
    r"(?i)Cisco IOS Software,[^\n]*Version\s+([^,\n]+)",
    r"(?i)\bVersion\s+([0-9A-Za-z.\(\)\-]+\d)(?:,\s*RELEASE|\s|$)",
    r"(?i)IOS[-\s]?XE Software,[^\n]*Version\s+([^,\n]+)",
]

def extract_ios_version_from_raw(raw_text: str):
    if not raw_text:
        return None
    for pat in _VERSION_PATTERNS:
        m = re.search(pat, raw_text)
        if m:
            ver = (m.group(1) or "").strip()
            if ver:
                return ver
    return None

def _recover_version_inplace(data: dict):
    parsed = data.get("parsed")
    raw = data.get("raw")
    if isinstance(parsed, dict):
        ver = parsed.get("version")
        if ver is None or (isinstance(ver, str) and not ver.strip()):
            recovered = extract_ios_version_from_raw(raw or "")
            if recovered:
                parsed["version"] = recovered

# ====================================================
#                   Pretty Printing
# ====================================================
def pretty_print_tool(name, data):
    # Attempt version recovery for get_version if not already done (safety)
    if name.startswith("get_version"):
        _recover_version_inplace(data)

    dev = data.get("device")
    header = f"{name}" if not dev else f"{name} [{dev}]"
    print(f"\n=== 🛠️  {header} ===")

    # Show server-side error if present
    if data.get("error"):
        print(f"❌ Server error: {data['error']}")

    if data.get("commands"):
        print("🔧 Commands to device:")
        for c in data["commands"]:
            print("  -", c)
    if data.get("raw") is not None:
        print("\n📡 RAW output:\n")
        print(data["raw"] if data["raw"] else "<no raw output>")
    if data.get("parsed") is not None:
        print("\n🧾 Parsed:")
        try:
            print(json.dumps(data["parsed"], indent=2))
        except TypeError:
            print(str(data["parsed"]))
    if data.get("saved") is not None:
        print(f"\n💾 Saved to NVRAM: {bool(data['saved'])}")
    if data.get("dry_run") is not None:
        print(f"🧪 Dry run: {bool(data['dry_run'])}")

# ====================================================
#                   Input Helpers
# ====================================================
def prompt_str(msg, default=None, allow_empty=False):
    d = f" [{default}]" if default is not None else ""
    while True:
        val = input(f"{msg}{d}: ").strip()
        if not val:
            if default is not None:
                return default
            if allow_empty:
                return ""
            print("Please enter a value.")
            continue
        return val

def prompt_bool(msg, default=True):
    d = "Y/n" if default else "y/N"
    while True:
        val = input(f"{msg} ({d}): ").strip().lower()
        if val == "":
            return default
        if val in ("y", "yes", "true", "1"):
            return True
        if val in ("n", "no", "false", "0"):
            return False
        print("Please answer y or n.")

def prompt_int(msg, default=None, min_val=None, max_val=None):
    d = f" [{default}]" if default is not None else ""
    while True:
        s = input(f"{msg}{d}: ").strip()
        if not s and default is not None:
            return int(default)
        try:
            v = int(s)
        except Exception:
            print("Please enter a number.")
            continue
        if min_val is not None and v < min_val:
            print(f"Must be >= {min_val}")
            continue
        if max_val is not None and v > max_val:
            print(f"Must be <= {max_val}")
            continue
        return v

def validate_ip_or_cidr(s):
    s = s.trim() if hasattr(s, "trim") else s.strip()
    if "/" in s:
        try:
            iface = ipaddress.ip_interface(s)
            return ("cidr", iface)
        except Exception:
            raise ValueError("Invalid CIDR (e.g., 10.0.0.1/24).")
    else:
        try:
            addr = ipaddress.ip_address(s)
            return ("ip", addr)
        except Exception:
            raise ValueError("Invalid IP address.")

def prompt_ip_with_optional_mask(default_ip, default_mask=None):
    while True:
        ip_in = prompt_str("IP (CIDR or dotted)", default_ip)
        try:
            kind, obj = validate_ip_or_cidr(ip_in)
        except ValueError as e:
            print(f"⚠️  {e}")
            continue
        if kind == "cidr":
            return str(obj), None
        mask_default = default_mask or "24"
        while True:
            mask = prompt_str("Mask (CIDR length like 24 or dotted like 255.255.255.0)", mask_default)
            try:
                if mask.isdigit():
                    _ = ipaddress.ip_network(f"0.0.0.0/{mask}", strict=False)
                else:
                    _ = ipaddress.ip_network(f"0.0.0.0/{mask}", strict=False)
                return str(obj), mask
            except Exception:
                print("⚠️  Invalid mask.")
                continue

# ====================================================
#           Device List (from server) & Picker
# ====================================================
def call_list_devices(client):
    """
    Uses the server's list_devices tool and returns a list of names.
    Expected: {"devices": ["R51","R52", ...]}
    Robust to both JSON content blocks and JSON text.
    """
    try:
        res = call_tool_raw(client, "list_devices", {}, timeout=30)
        # direct dict format
        if isinstance(res, dict) and "devices" in res and isinstance(res["devices"], list):
            return res["devices"]
        # rare case: 'raw' contains JSON
        raw = res.get("raw") if isinstance(res, dict) else None
        if raw:
            try:
                j = json.loads(raw)
                if isinstance(j, dict) and "devices" in j and isinstance(j["devices"], list):
                    return j["devices"]
                if isinstance(j, list):
                    return j
            except Exception:
                pass
        return []
    except Exception as e:
        print(f"⚠️  list_devices failed: {e}")
        return []

def pick_device_or_all(client, allow_all=True, prompt_label="Selection"):
    names = call_list_devices(client)
    if not names:
        print("⚠️  Could not retrieve device list from server; using server default device.")
        return {"mode": "single", "device": None}  # None → server default

    print("\n🗂️  Devices:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    if allow_all:
        print("\nPick a device number or name, press ENTER / 'a' for all, or 'q' to cancel.")
    else:
        print("\nPick a device number or name (ENTER picks the first) or 'q' to cancel.")

    sel = input(f"{prompt_label}: ").strip()

    # cancel/back to main
    if sel.lower() in ("q", "quit", "exit"):
        return {"mode": "cancel"}

    if allow_all and (sel == "" or sel.lower() in ("a", "all")):
        return {"mode": "all", "names": names}

    if sel == "":
        # no 'all' allowed; default to first device
        return {"mode": "single", "device": names[0]}

    # Number?
    if sel.isdigit():
        idx = int(sel)
        if 1 <= idx <= len(names):
            return {"mode": "single", "device": names[idx-1]}
        print("⚠️  Out of range; defaulting to first device.")
        return {"mode": "single", "device": names[0]}

    # Name?
    if sel in names:
        return {"mode": "single", "device": sel}

    print("⚠️  Not recognized; defaulting to first device.")
    return {"mode": "single", "device": names[0]}

# ====================================================
#               Wizards for Tools 3 & 4
# ====================================================
def wizard_set_interface_ip(client):
    print("\n🔧 set_interface_ip — guided setup")
    sel = pick_device_or_all(client, allow_all=False, prompt_label="Device")
    if sel["mode"] == "cancel":
        return None

    # Tip for your platform naming
    print("ℹ️  Tip: On your device, use names like 'Ethernet0/0', 'Ethernet0/1', 'Ethernet0/2', 'Ethernet0/3'.")

    iface = prompt_str("Interface", os.getenv("TARGET_IFACE", "Ethernet0/0"))
    ip_in, mask = prompt_ip_with_optional_mask(os.getenv("TARGET_IP", "10.10.10.1/24"), os.getenv("TARGET_MASK"))
    replace = prompt_bool("Replace existing IP on interface?", os.getenv("REPLACE", "1") == "1")
    no_shutdown = prompt_bool("Send 'no shutdown'?", os.getenv("NO_SHUT", "1") == "1")
    save = prompt_bool("Save config (write memory)?", os.getenv("SAVE", "0") == "1")
    dry_run = prompt_bool("Dry run (preview only)?", os.getenv("DRY_RUN", "1") == "1")

    args = {
        "interface": iface,
        "ip": ip_in,
        "replace": replace,
        "no_shutdown": no_shutdown,
        "save": save,
        "dry_run": dry_run,
    }
    if mask:
        args["mask"] = mask
    if sel.get("device"):
        args["device"] = sel["device"]

    print("\n📋 Review arguments:")
    print(json.dumps(args, indent=2))
    if not prompt_bool("Proceed with these settings?", True):
        return wizard_set_interface_ip(client)
    return args

def wizard_create_loopback(client):
    print("\n🔧 create_loopback — guided setup")
    sel = pick_device_or_all(client, allow_all=False, prompt_label="Device")
    if sel["mode"] == "cancel":
        return None

    # Default Loopback ID 0 (widely supported); you can choose any integer >= 0
    loop_id = prompt_int("Loopback ID", int(os.getenv("LOOPBACK_ID", "0")), min_val=0)
    ip_in, mask = prompt_ip_with_optional_mask(os.getenv("LOOPBACK_IP", "192.0.2.100/32"))
    desc = prompt_str("Description", os.getenv("LOOPBACK_DESC", "MCP-created loopback"), allow_empty=True)
    save = prompt_bool("Save config (write memory)?", os.getenv("SAVE", "0") == "1")
    dry_run = prompt_bool("Dry run (preview only)?", os.getenv("DRY_RUN", "1") == "1")

    args = {
        "loopback_id": loop_id,
        "ip": ip_in,
        "description": desc,
        "save": save,
        "dry_run": dry_run,
    }
    if mask:
        args["mask"] = mask
    if sel.get("device"):
        args["device"] = sel["device"]

    print("\n📋 Review arguments:")
    print(json.dumps(args, indent=2))
    if not prompt_bool("Proceed with these settings?", True):
        return wizard_create_loopback(client)
    return args

# ====================================================
#                       Menu
# ====================================================
def interactive_menu(tool_names, tool_map):
    while True:
        print("\n===== Select a tool (or 'q' to quit) =====")
        for i, name in enumerate(tool_names, 1):
            desc = tool_map.get(name, {}).get("description", "")
            print(f"{i}. {name}" + (f" — {desc}" if desc else ""))

        choice_raw = input("\nChoice: ")
        if choice_raw is None:
            return None
        choice = choice_raw.strip()

        if choice.lower() in ("q", "quit", "exit"):
            return None

        m = re.match(r"^\s*(\d+)\s*\.?\s*$", choice)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(tool_names):
                return tool_names[idx]
            print("❌ Invalid number.")
            continue

        if choice in tool_names:
            return choice
        lowered = {n.lower(): n for n in tool_names}
        if choice.lower() in lowered:
            return lowered[choice.lower()]
        print(f"❌ Unknown selection '{choice_raw}'. Try a number like '1' or a tool name.")

# ====================================================
#                       Main
# ====================================================
if __name__ == "__main__":
    try:
        client = MCPClient(SERVER_CMD)

        # 1) Initialize
        init = client.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "simple-mcp-client", "version": "6.2-loopback-fallback"}
        }, timeout=25)
        server_name = init["result"]["serverInfo"]["name"]
        print(f"✅ Connected to MCP server: {server_name}")

        # 2) Notify initialized
        client.notify("notifications/initialized", {})
        time.sleep(0.4)

        # 3) List tools
        resp = client.request("tools/list", None, timeout=10)
        tools = resp.get("result", {}).get("tools", [])
        if not tools:
            print("❌ No tools exposed by server.")
            sys.exit(1)
        tool_names = [t["name"] for t in tools]
        tool_map = {t["name"]: t for t in tools}
        print("🧰 Tools available:", ", ".join(tool_names))

        # 4) Loop
        while True:
            selected = interactive_menu(tool_names, tool_map)
            if not selected:
                print("👋 Bye.")
                break

            # 1) list_devices: run as-is
            if selected == "list_devices":
                try:
                    data = call_tool_norm(client, "list_devices", {}, timeout=60)
                    # list_devices returns {"devices": [...]}; print nicely
                    if "devices" in data and isinstance(data["devices"], list):
                        print("\n🗂️  Devices:")
                        for i, n in enumerate(data["devices"], 1):
                            print(f"  {i}. {n}")
                    pretty_print_tool("list_devices", data)
                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                continue

            # 2) get_interfaces — allow all or single; handle cancel
            if selected == "get_interfaces":
                sel = pick_device_or_all(client, allow_all=True, prompt_label="Selection")
                if sel["mode"] == "cancel":
                    continue

                # ALL devices aggregation
                if sel["mode"] == "all":
                    names = sel["names"]
                    combined_raw = []
                    combined_parsed = []
                    print("\n▶️  Running 'get_interfaces' on ALL devices ...")
                    for name in names:
                        try:
                            data = call_tool_norm(client, "get_interfaces", {"device": name}, timeout=90)
                            pretty_print_tool("get_interfaces", data)
                            r = data.get("raw") or ""
                            p = data.get("parsed")
                            combined_raw.append(f"=== {name} ===\n{r}\n")
                            combined_parsed.append({"device": name, "parsed": p})
                        except Exception as e:
                            print(f"❌ {name}: {e}")
                            combined_parsed.append({"device": name, "error": str(e)})

                    print("\n=== 📦 Aggregated (all devices) ===")
                    print("\n📡 RAW (combined):\n")
                    print("\n".join(combined_raw))
                    print("\n🧾 Parsed (combined):")
                    print(json.dumps(combined_parsed, indent=2))
                    continue

                # Single device
                args = {}
                if sel.get("device"):
                    args["device"] = sel["device"]
                print(f"\n▶️  Running 'get_interfaces' with {args or {'device':'<default>'}} ...")
                try:
                    data = call_tool_norm(client, "get_interfaces", args, timeout=90)
                    pretty_print_tool("get_interfaces", data)
                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                continue

            # 3) get_version — allow all or single; handle cancel, with version recovery
            if selected == "get_version":
                sel = pick_device_or_all(client, allow_all=True, prompt_label="Selection")
                if sel["mode"] == "cancel":
                    continue

                if sel["mode"] == "all":
                    names = sel["names"]
                    combined_raw = []
                    combined_parsed = []
                    print("\n▶️  Running 'get_version' on ALL devices ...")
                    for name in names:
                        try:
                            data = call_tool_norm(client, "get_version", {"device": name}, timeout=90)
                            pretty_print_tool("get_version", data)  # recovery runs in call_tool_norm/pretty_print
                            r = data.get("raw") or ""
                            p = data.get("parsed")
                            combined_raw.append(f"=== {name} ===\n{r}\n")
                            combined_parsed.append({"device": name, "parsed": p})
                        except Exception as e:
                            print(f"❌ {name}: {e}")
                            combined_parsed.append({"device": name, "error": str(e)})

                    print("\n=== 📦 Aggregated (all devices) ===")
                    print("\n📡 RAW (combined):\n")
                    print("\n".join(combined_raw))
                    print("\n🧾 Parsed (combined):")
                    print(json.dumps(combined_parsed, indent=2))
                else:
                    args = {"device": sel["device"]} if sel.get("device") else {}
                    print(f"\n▶️  Running 'get_version' with {args or {'device':'<default>'}} ...")
                    try:
                        data = call_tool_norm(client, "get_version", args, timeout=90)
                        pretty_print_tool("get_version", data)  # recovery runs here too
                    except Exception as e:
                        print(f"❌ Tool call failed: {e}")
                continue

            # 4) set_interface_ip — guided wizard (single device)
            if selected == "set_interface_ip":
                args = wizard_set_interface_ip(client)
                if args is None:  # canceled
                    continue
                try:
                    data = call_tool_norm(client, "set_interface_ip", args, timeout=120)
                    pretty_print_tool("set_interface_ip", data)
                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                continue

            # 5) create_loopback — guided wizard (single device) with graceful fallback
            if selected == "create_loopback":
                args = wizard_create_loopback(client)
                if args is None:  # canceled
                    continue
                try:
                    data = call_tool_norm(client, "create_loopback", args, timeout=120)
                    pretty_print_tool("create_loopback", data)

                    # If server reported an error, offer fallback to set_interface_ip on a physical interface
                    if data.get("error"):
                        print("\n⚠️  Loopback creation failed on the server.")
                        if prompt_bool("Try applying the same IP to a physical interface instead?", True):
                            # Build fallback args for set_interface_ip
                            # Reuse device from loopback args, reuse IP/mask (CIDR ok)
                            fb = {
                                "ip": args["ip"],
                                "replace": True,
                                "no_shutdown": True,
                                "save": False,
                                "dry_run": False,
                            }
                            if "mask" in args and args["mask"]:
                                fb["mask"] = args["mask"]
                            if "device" in args and args["device"]:
                                fb["device"] = args["device"]

                            print("ℹ️  Your platform uses names like 'Ethernet0/0' .. 'Ethernet0/3'.")
                            fb_iface = prompt_str("Interface to configure (e.g., Ethernet0/0)", "Ethernet0/0")
                            fb["interface"] = fb_iface

                            print("\n📋 Fallback arguments (set_interface_ip):")
                            print(json.dumps(fb, indent=2))
                            if prompt_bool("Proceed with fallback?", True):
                                try:
                                    fb_result = call_tool_norm(client, "set_interface_ip", fb, timeout=120)
                                    pretty_print_tool("set_interface_ip (fallback)", fb_result)
                                except Exception as ee:
                                    print(f"❌ Fallback failed: {ee}")

                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                continue

            print("No custom flow for this tool yet.")

    except KeyboardInterrupt:
        print("\n🛑 Interrupted.")
    except Exception as e:
        print(f"💥 Fatal error: {e}")
        sys.exit(2)
