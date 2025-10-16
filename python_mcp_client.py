#!/usr/bin/env python3
import json, subprocess, threading, time, os, sys, re, ipaddress
from pathlib import Path

# Run your FastMCP server script
SERVER_CMD = ["python3", "mcp_server.py"]

# ---------------- MCP wire ----------------
class MCPClient:
    def __init__(self, cmd):
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
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

    def request(self, method, params=None, timeout=20):
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

# ---------------- Helpers: results & printing ----------------
def extract_all(call_result):
    """Normalize tool outputs into one dict."""
    result = call_result.get("result", {})
    blocks = result.get("content", [])
    merged = {"raw": None, "parsed": None, "commands": None, "saved": None, "dry_run": None}
    for b in blocks:
        if b.get("type") == "json":
            data = b.get("data", {})
            for k in merged.keys():
                if k in data and data[k] is not None:
                    merged[k] = data[k]
    if all(v is None for v in merged.values()):
        for b in blocks:
            if b.get("type") == "text":
                try:
                    j = json.loads(b["text"])
                    for k in merged.keys():
                        if k in j and j[k] is not None:
                            merged[k] = j[k]
                except Exception:
                    pass
    return merged

def pretty_print_tool(name, data):
    print(f"\n=== 🛠️  {name} ===")
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

def call_tool(client, tool_name, arguments=None, timeout=60):
    arguments = arguments or {}
    call = client.request("tools/call", {"name": tool_name, "arguments": arguments}, timeout=timeout)
    if "error" in call:
        raise RuntimeError(f"Tool '{tool_name}' error: {call['error']}")
    return extract_all(call)

# ---------------- Defaults ----------------
def defaults_for(tool_name):
    if tool_name == "get_interfaces":
        return {}
    if tool_name == "get_version":
        return {}
    if tool_name == "set_interface_ip":
        out = {
            "interface": os.getenv("TARGET_IFACE", "GigabitEthernet1"),
            "ip": os.getenv("TARGET_IP", "10.10.10.1/24"),
            "replace": os.getenv("REPLACE", "1") == "1",
            "no_shutdown": os.getenv("NO_SHUT", "1") == "1",
            "save": os.getenv("SAVE", "0") == "1",
            "dry_run": os.getenv("DRY_RUN", "1") == "1",
        }
        m = os.getenv("TARGET_MASK", "").strip()
        if m:
            out["mask"] = m
        return out
    if tool_name == "create_loopback":
        return {
            "loopback_id": int(os.getenv("LOOPBACK_ID", "100")),
            "ip": os.getenv("LOOPBACK_IP", "192.0.2.100/32"),
            "description": os.getenv("LOOPBACK_DESC", "MCP-created loopback"),
            "save": os.getenv("SAVE", "0") == "1",
            "dry_run": os.getenv("DRY_RUN", "1") == "1",
        }
    return {}

def tool_requires_args(schema, defaults):
    """Auto-run only when nothing is required and defaults are {}."""
    if not schema:
        return bool(defaults)
    try:
        req = schema.get("required", [])
        if isinstance(req, list) and len(req) > 0:
            return True
        return bool(defaults)
    except Exception:
        return bool(defaults)

# ---------------- Input helpers (wizard) ----------------
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
        if val == "" or val is None:
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
    """Return ('cidr', ip_interface) or ('ip', ip_address). Raise on error."""
    s = s.strip()
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
            return str(obj), None  # mask will be derived by server
        # Need a mask if not CIDR
        mask_default = default_mask or "24"
        while True:
            mask = prompt_str("Mask (CIDR length like 24 or dotted like 255.255.255.0)", mask_default)
            # Validate mask (CIDR or dotted)
            try:
                if mask.isdigit():
                    _ = ipaddress.ip_network(f"0.0.0.0/{mask}", strict=False)
                else:
                    _ = ipaddress.ip_network(f"0.0.0.0/{mask}", strict=False)
                return str(obj), mask
            except Exception:
                print("⚠️  Invalid mask.")
                continue

# ---------------- Wizards for tools 3 & 4 ----------------
def wizard_set_interface_ip():
    print("\n🔧 set_interface_ip — guided setup")
    d = defaults_for("set_interface_ip")
    iface = prompt_str("Interface", d.get("interface", "GigabitEthernet1"))
    ip_in, mask = prompt_ip_with_optional_mask(d.get("ip", "10.10.10.1/24"), d.get("mask"))
    replace = prompt_bool("Replace existing IP on interface?", d.get("replace", True))
    no_shutdown = prompt_bool("Send 'no shutdown'?", d.get("no_shutdown", True))
    save = prompt_bool("Save config (write memory)?", d.get("save", False))
    dry_run = prompt_bool("Dry run (don't apply config, just preview)?", d.get("dry_run", True))

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

    print("\n📋 Review arguments:")
    print(json.dumps(args, indent=2))
    if not prompt_bool("Proceed with these settings?", True):
        print("↩️  Restarting wizard...")
        return wizard_set_interface_ip()
    return args

def wizard_create_loopback():
    print("\n🔧 create_loopback — guided setup")
    d = defaults_for("create_loopback")
    loop_id = prompt_int("Loopback ID", d.get("loopback_id", 100), min_val=0)
    ip_in, mask = prompt_ip_with_optional_mask(d.get("ip", "192.0.2.100/32"))
    desc = prompt_str("Description", d.get("description", "MCP-created loopback"), allow_empty=True)
    save = prompt_bool("Save config (write memory)?", d.get("save", False))
    dry_run = prompt_bool("Dry run (don't apply config, just preview)?", d.get("dry_run", True))

    args = {
        "loopback_id": loop_id,
        "ip": ip_in,
        "description": desc,
        "save": save,
        "dry_run": dry_run,
    }
    if mask:
        args["mask"] = mask

    print("\n📋 Review arguments:")
    print(json.dumps(args, indent=2))
    if not prompt_bool("Proceed with these settings?", True):
        print("↩️  Restarting wizard...")
        return wizard_create_loopback()
    return args

# ---------------- Menu ----------------
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

# ---------------- Main ----------------
if __name__ == "__main__":
    try:
        client = MCPClient(SERVER_CMD)

        # 1) Initialize
        init = client.request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "simple-mcp-client", "version": "3.3-interactive-wizard"}
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

            # For 1 & 2: auto-run (no args). For 3 & 4: wizard.
            if selected in ("get_interfaces", "get_version"):
                schema = tool_map.get(selected, {}).get("inputSchema")
                defaults = defaults_for(selected)
                if not tool_requires_args(schema, defaults):
                    print(f"\n▶️  Running '{selected}' with default arguments {{}} ...")
                    try:
                        data = call_tool(client, selected, {}, timeout=60)
                        pretty_print_tool(selected, data)
                    except Exception as e:
                        print(f"❌ Tool call failed: {e}")
                    continue

            if selected == "set_interface_ip":
                args = wizard_set_interface_ip()
                try:
                    data = call_tool(client, selected, args, timeout=90)
                    pretty_print_tool(selected, data)
                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                continue

            if selected == "create_loopback":
                args = wizard_create_loopback()
                try:
                    data = call_tool(client, selected, args, timeout=90)
                    pretty_print_tool(selected, data)
                except Exception as e:
                    print(f"❌ Tool call failed: {e}")
                continue

            # Any other tools (if added later): fallback to JSON prompt
            schema = tool_map.get(selected, {}).get("inputSchema")
            if schema:
                print("\n📐 Input schema (hint):")
                try:
                    print(json.dumps(schema, indent=2))
                except Exception:
                    print(schema)
            defaults = defaults_for(selected)
            print("(Enter JSON args or hit ENTER for defaults)")
            print(json.dumps(defaults, indent=2))
            raw = input("> ").strip()
            args = defaults if not raw else json.loads(raw)

            try:
                data = call_tool(client, selected, args, timeout=60)
                pretty_print_tool(selected, data)
            except Exception as e:
                print(f"❌ Tool call failed: {e}")

    except KeyboardInterrupt:
        print("\n🛑 Interrupted.")
    except Exception as e:
        print(f"💥 Fatal error: {e}")
        sys.exit(2)

