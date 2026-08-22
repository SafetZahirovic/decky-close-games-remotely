import os
import json
import asyncio
import signal
import socket
import struct
import urllib.request
import decky

PLUGIN_PORT = 55123
SETTINGS_FILE = "settings.json"


def load_settings():
    path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, SETTINGS_FILE)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"devices": [], "tv_ip": "", "plugin_port": PLUGIN_PORT}


def save_settings(settings):
    path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, SETTINGS_FILE)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)


# ---------------------------------------------------------------------------
# Minimal async HTTP server (no dependencies)
# ---------------------------------------------------------------------------
class MiniHTTPServer:
    def __init__(self, host="0.0.0.0", port=PLUGIN_PORT):
        self.host = host
        self.port = port
        self.routes: dict[tuple[str, str], object] = {}
        self.server = None

    def route(self, method: str, path: str):
        def decorator(handler):
            self.routes[(method.upper(), path)] = handler
            return handler
        return decorator

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        decky.logger.info(f"HTTP server listening on {self.host}:{self.port}")

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            decky.logger.info("HTTP server stopped")

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return

            parts = request_line.decode().strip().split(" ", 2)
            if len(parts) < 2:
                return
            method, path = parts[0], parts[1]

            # Read headers
            content_length = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                header = line.decode().strip()
                if header.lower().startswith("content-length:"):
                    content_length = int(header.split(":", 1)[1].strip())

            # Read body
            body = None
            if content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=10)

            # Dispatch
            handler = self.routes.get((method.upper(), path))
            if handler:
                result = await handler(body)
                response_body = json.dumps(result).encode()
                status_line = b"HTTP/1.1 200 OK\r\n"
            else:
                response_body = b'{"error":"not found"}'
                status_line = b"HTTP/1.1 404 Not Found\r\n"

            writer.write(
                status_line
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(response_body)}\r\n".encode()
                + b"Access-Control-Allow-Origin: *\r\n"
                + b"Connection: close\r\n"
                + b"\r\n"
                + response_body
            )
            await writer.drain()
        except Exception as e:
            decky.logger.error(f"HTTP handler error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def resolve_mac(ip: str) -> str | None:
    """Resolve MAC address from IP via ARP table. Pings first to ensure ARP entry exists."""
    import subprocess
    try:
        # Ping to populate ARP cache (device must be online for this to work)
        subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
    except Exception:
        pass
    try:
        # Read ARP table
        result = subprocess.run(["ip", "neigh", "show", ip],
                                capture_output=True, text=True, timeout=5)
        # Format: "192.168.1.10 dev eth0 lladdr aa:bb:cc:dd:ee:ff REACHABLE"
        for line in result.stdout.strip().split("\n"):
            if "lladdr" in line:
                parts = line.split()
                idx = parts.index("lladdr")
                return parts[idx + 1]
    except Exception:
        pass
    # Fallback: try /proc/net/arp (older systems)
    try:
        with open("/proc/net/arp", "r") as f:
            for line in f:
                if line.startswith(ip + " ") or line.startswith(ip + "\t"):
                    parts = line.split()
                    if len(parts) >= 4 and parts[3] != "00:00:00:00:00:00":
                        return parts[3]
    except Exception:
        pass
    return None


def get_local_mac() -> str:
    """Get the MAC address of the default network interface."""
    import subprocess
    try:
        # Get the default route interface
        result = subprocess.run(
            ["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5
        )
        iface = None
        for part in result.stdout.split():
            if part == "dev":
                continue
            # The word after "dev" is the interface name
        parts = result.stdout.split()
        if "dev" in parts:
            iface = parts[parts.index("dev") + 1]
        if iface:
            with open(f"/sys/class/net/{iface}/address", "r") as f:
                return f.read().strip()
    except Exception:
        pass
    # Fallback: first non-lo interface
    try:
        for name in os.listdir("/sys/class/net"):
            if name == "lo":
                continue
            with open(f"/sys/class/net/{name}/address", "r") as f:
                mac = f.read().strip()
                if mac and mac != "00:00:00:00:00:00":
                    return mac
    except Exception:
        pass
    return ""


def send_wol(mac_address: str, host_ip: str = "", port: int = 9):
    """Send Wake-on-LAN magic packets to multiple destinations (like MoonDeck)."""
    mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16

    targets = []

    # Always send to global broadcast
    targets.append(("255.255.255.255", socket.AF_INET))

    # Also send directly to the host IP (works even when device is off
    # because the switch/router may still have the MAC in its table)
    if host_ip:
        try:
            for info in socket.getaddrinfo(host_ip, port, family=socket.AF_UNSPEC):
                family, _, _, _, sockaddr = info
                if family in (socket.AF_INET, socket.AF_INET6):
                    targets.append((sockaddr[0], family))
        except socket.gaierror:
            targets.append((host_ip, socket.AF_INET))

    for addr, family in targets:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.connect((addr, port))
                sock.send(magic)
                decky.logger.info(f"WOL packet sent to {addr}:{port}")
        except OSError as err:
            if err.errno in (101, 65):  # ENETUNREACH (Linux / macOS)
                decky.logger.warning(f"WOL to {addr} failed: {err}")
            else:
                raise


def get_running_games() -> list[dict]:
    """Find ALL running Steam game processes, grouped by app ID.
    Also collects process group IDs so we can kill entire process trees."""
    apps: dict[str, dict] = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/environ", "rb") as f:
                    environ = f.read()
                if b"SteamAppId=" not in environ:
                    continue
                app_id = None
                for var in environ.split(b"\x00"):
                    if var.startswith(b"SteamAppId="):
                        app_id = var.split(b"=", 1)[1].decode()
                        break
                if not app_id or app_id == "0":
                    continue
                pid = int(entry)
                # Get process group ID
                try:
                    pgid = os.getpgid(pid)
                except OSError:
                    pgid = None
                if app_id not in apps:
                    name = "unknown"
                    try:
                        with open(f"/proc/{entry}/comm", "r") as f:
                            name = f.read().strip()
                    except Exception:
                        pass
                    apps[app_id] = {"app_id": app_id, "name": name, "pids": [], "pgids": set()}
                apps[app_id]["pids"].append(pid)
                if pgid:
                    apps[app_id]["pgids"].add(pgid)
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception:
        pass
    # Convert pgids sets to lists for JSON serialization
    for app in apps.values():
        app["pgids"] = list(app["pgids"])
    return list(apps.values())


def _kill_games(games: list[dict], sig: int):
    """Kill games by process group first (catches all children), then individual PIDs as fallback."""
    sig_name = signal.Signals(sig).name
    killed_pgids: set[int] = set()

    # Phase A: Kill entire process groups (this catches Wine/Proton/launcher/child processes)
    for game in games:
        for pgid in game.get("pgids", []):
            if pgid in killed_pgids or pgid <= 1:
                continue
            try:
                os.killpg(pgid, sig)
                killed_pgids.add(pgid)
                decky.logger.info(f"Sent {sig_name} to process group {pgid} ({game['name']}, AppId={game['app_id']})")
            except ProcessLookupError:
                pass
            except PermissionError:
                decky.logger.warning(f"Permission denied killing pgid {pgid}")
            except Exception as e:
                decky.logger.error(f"Failed to kill pgid {pgid}: {e}")

    # Phase B: Kill individual PIDs that might have been missed
    for game in games:
        for pid in game["pids"]:
            try:
                os.kill(pid, sig)
                decky.logger.info(f"Sent {sig_name} to PID {pid} ({game['name']})")
            except ProcessLookupError:
                pass  # Already dead from group kill
            except Exception as e:
                decky.logger.error(f"Failed to kill PID {pid}: {e}")


async def close_all_games() -> dict:
    """Close all running Steam games with multiple rounds of scan-and-kill."""
    initial_games = get_running_games()
    if not initial_games:
        return {"status": "ok", "closed": [], "total_pids": 0}

    total_pids = sum(len(g["pids"]) for g in initial_games)
    game_names = [g["name"] for g in initial_games]
    decky.logger.info(f"Closing {len(initial_games)} game(s) ({total_pids} processes): {game_names}")

    # Round 1: SIGTERM process groups + PIDs
    _kill_games(initial_games, signal.SIGTERM)

    for _ in range(10):
        await asyncio.sleep(1)
        if not get_running_games():
            decky.logger.info("All games exited after SIGTERM")
            return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGTERM"}

    # Round 2: Re-scan (catches respawned/new processes) and SIGTERM again
    respawned = get_running_games()
    if respawned:
        decky.logger.warning(f"Games still running after first SIGTERM, re-scanning and killing...")
        _kill_games(respawned, signal.SIGTERM)
        for _ in range(5):
            await asyncio.sleep(1)
            if not get_running_games():
                decky.logger.info("All games exited after second SIGTERM")
                return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGTERM"}

    # Round 3: SIGKILL everything still alive
    remaining = get_running_games()
    if remaining:
        rem_pids = sum(len(g["pids"]) for g in remaining)
        decky.logger.warning(f"SIGKILL {len(remaining)} game(s) ({rem_pids} processes)")
        _kill_games(remaining, signal.SIGKILL)
        for _ in range(5):
            await asyncio.sleep(1)
            if not get_running_games():
                decky.logger.info("All games exited after SIGKILL")
                return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGKILL"}

    still = get_running_games()
    if still:
        return {"status": "partial", "closed": initial_games, "remaining": still, "total_pids": total_pids}
    return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGKILL"}


def http_get(url: str, timeout: int = 5) -> dict | None:
    """Synchronous HTTP GET, returns parsed JSON or None."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def http_post(url: str, data: dict | None = None, timeout: int = 60) -> dict | None:
    """Synchronous HTTP POST, returns parsed JSON or None."""
    try:
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LG WebOS TV control (minimal WebSocket implementation)
# ---------------------------------------------------------------------------
import hashlib
import base64
import ssl


async def _ws_connect(tv_ip: str) -> tuple:
    """Try to open a WebSocket connection to the TV.
    Tries port 3001 (SSL) first (newer TVs require it), then port 3000 (plain)."""
    errors = []

    # Try SSL first (port 3001) — most modern LG TVs require this
    for port, use_ssl in [(3001, True), (3000, False)]:
        try:
            if use_ssl:
                ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
                # Allow older TLS versions for compatibility with older TV firmware
                ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(tv_ip, port, ssl=ssl_ctx), timeout=5
                )
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(tv_ip, port), timeout=5
                )

            # WebSocket upgrade handshake
            ws_key = base64.b64encode(os.urandom(16)).decode()
            handshake = (
                f"GET / HTTP/1.1\r\n"
                f"Host: {tv_ip}:{port}\r\n"
                f"Origin: https://{tv_ip}:{port}\r\n"
                f"Upgrade: websocket\r\n"
                f"Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {ws_key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"\r\n"
            )
            writer.write(handshake.encode())
            await writer.drain()

            response = await asyncio.wait_for(reader.read(4096), timeout=5)
            if b"101" in response:
                decky.logger.info(f"WebSocket connected to TV on port {port} ({'SSL' if use_ssl else 'plain'})")
                return reader, writer, None
            else:
                resp_line = response.split(b"\r\n")[0].decode(errors="replace")
                msg = f"Port {port} ({'SSL' if use_ssl else 'plain'}): handshake rejected ({resp_line})"
                decky.logger.warning(msg)
                errors.append(msg)
                writer.close()
        except Exception as e:
            msg = f"Port {port} ({'SSL' if use_ssl else 'plain'}): {e}"
            decky.logger.warning(f"TV connection failed — {msg}")
            errors.append(msg)
            continue

    return None, None, "; ".join(errors)


def _ws_make_send(writer):
    """Create a WebSocket send function."""
    async def ws_send(data: str):
        payload = data.encode()
        frame = bytearray()
        frame.append(0x81)  # FIN + text
        length = len(payload)
        mask_key = os.urandom(4)
        if length < 126:
            frame.append(0x80 | length)
        elif length < 65536:
            frame.append(0x80 | 126)
            frame.extend(struct.pack(">H", length))
        else:
            frame.append(0x80 | 127)
            frame.extend(struct.pack(">Q", length))
        frame.extend(mask_key)
        masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        frame.extend(masked)
        writer.write(bytes(frame))
        await writer.drain()
    return ws_send


def _ws_make_recv(reader):
    """Create a WebSocket receive function that handles masked/binary/ping frames."""
    async def ws_recv() -> str:
        while True:
            header = await asyncio.wait_for(reader.readexactly(2), timeout=10)
            opcode = header[0] & 0x0F
            masked = bool(header[1] & 0x80)
            length = header[1] & 0x7F

            if length == 126:
                ext = await reader.readexactly(2)
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = await reader.readexactly(8)
                length = struct.unpack(">Q", ext)[0]

            # Read mask key if present
            mask_key = None
            if masked:
                mask_key = await reader.readexactly(4)

            data = await asyncio.wait_for(reader.readexactly(length), timeout=10) if length > 0 else b""

            # Unmask if needed
            if mask_key and data:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))

            # Handle different opcodes
            if opcode == 0x01:  # Text frame
                return data.decode("utf-8", errors="replace")
            elif opcode == 0x02:  # Binary frame — try to decode as text
                return data.decode("utf-8", errors="replace")
            elif opcode == 0x08:  # Close
                return ""
            elif opcode == 0x09:  # Ping — ignore and read next frame
                continue
            elif opcode == 0x0A:  # Pong — ignore and read next frame
                continue
            else:
                return data.decode("utf-8", errors="replace")
    return ws_recv


async def turn_off_lg_tv(tv_ip: str, client_key: str = "", tv_mac: str = "") -> dict:
    """Turn off an LG WebOS TV using the SSAP protocol over WebSocket.
    Tries port 3000 (plain) and 3001 (SSL). Optionally wakes the TV first via WOL."""
    # If TV is unreachable, try waking it first (some LG TVs support WOL in standby)
    reader, writer, errors = await _ws_connect(tv_ip)

    if not reader and tv_mac:
        decky.logger.info(f"TV unreachable, trying WOL to {tv_mac}...")
        send_wol(tv_mac, host_ip=tv_ip)
        await asyncio.sleep(5)
        reader, writer, errors = await _ws_connect(tv_ip)

    if not reader:
        return {"status": "error", "message": f"Cannot connect to TV at {tv_ip}. {errors}"}

    try:
        ws_send = _ws_make_send(writer)
        ws_recv = _ws_make_recv(reader)

        # Register with the TV
        register_payload = {
            "type": "register",
            "id": "register_0",
            "payload": {
                "forcePairing": False,
                "pairingType": "PROMPT",
                "manifest": {
                    "manifestVersion": 1,
                    "appVersion": "1.1",
                    "signed": {
                        "created": "20140509",
                        "appId": "com.lge.test",
                        "vendorId": "com.lge",
                        "localizedAppNames": {"": "Decky Remote"},
                    },
                    "permissions": ["CONTROL_POWER"],
                    "signatures": [{"signatureVersion": 1, "signature": ""}],
                },
            },
        }
        if client_key:
            register_payload["payload"]["client-key"] = client_key

        await ws_send(json.dumps(register_payload))

        # Wait for registration response (may need TV prompt acceptance on first pair)
        for _ in range(60):
            msg = await ws_recv()
            if not msg:
                continue
            data = json.loads(msg)
            if data.get("type") == "registered":
                new_key = data.get("payload", {}).get("client-key", client_key)
                power_off = {
                    "type": "request",
                    "id": "power_off",
                    "uri": "ssap://system/turnOff",
                }
                await ws_send(json.dumps(power_off))
                writer.close()
                return {"status": "ok", "client_key": new_key}
            elif data.get("type") == "error":
                writer.close()
                return {"status": "error", "message": data.get("error", "Unknown error")}

        writer.close()
        return {"status": "error", "message": "Registration timed out — accept pairing on TV?"}
    except Exception as e:
        try:
            writer.close()
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------
class Plugin:
    http_server: MiniHTTPServer | None = None
    settings: dict = {}

    # ---- Lifecycle --------------------------------------------------------

    async def _main(self):
        self.settings = load_settings()
        port = self.settings.get("plugin_port", PLUGIN_PORT)

        # Set up HTTP server for receiving remote commands
        self.http_server = MiniHTTPServer(port=port)

        @self.http_server.route("GET", "/ping")
        async def handle_ping(_body):
            # Include this device's MAC so the remote plugin can store it
            mac = get_local_mac()
            return {"status": "ok", "hostname": socket.gethostname(), "mac": mac}

        @self.http_server.route("GET", "/status")
        async def handle_status(_body):
            games = get_running_games()
            return {"status": "ok", "games": games, "hostname": socket.gethostname()}

        @self.http_server.route("POST", "/close-all-games")
        async def handle_close_games(_body):
            result = await close_all_games()
            return result

        @self.http_server.route("POST", "/shutdown")
        async def handle_shutdown(_body):
            # Schedule shutdown after sending response
            asyncio.get_event_loop().call_later(3, lambda: os.system("systemctl poweroff"))
            return {"status": "shutting_down"}

        @self.http_server.route("POST", "/suspend")
        async def handle_suspend(_body):
            asyncio.get_event_loop().call_later(3, lambda: os.system("systemctl suspend"))
            return {"status": "suspending"}

        await self.http_server.start()
        decky.logger.info("Close Games Remotely plugin loaded")

    async def _unload(self):
        if self.http_server:
            await self.http_server.stop()
        save_settings(self.settings)
        decky.logger.info("Close Games Remotely plugin unloaded")

    # ---- Settings ---------------------------------------------------------

    async def get_settings(self) -> dict:
        return self.settings

    async def save_all_settings(self, settings: dict) -> bool:
        self.settings = settings
        save_settings(settings)
        return True

    async def add_device(self, name: str, ip: str, mac: str = "") -> dict:
        loop = asyncio.get_event_loop()
        # Try to auto-resolve MAC if not provided (best-effort, not required)
        if not mac:
            # Best method: ask the remote plugin for its own MAC
            ping_result = await loop.run_in_executor(
                None, http_get, f"http://{ip}:{PLUGIN_PORT}/ping"
            )
            if ping_result and ping_result.get("mac"):
                mac = ping_result["mac"]
                decky.logger.info(f"Got MAC from remote plugin for {ip}: {mac}")
            # MAC is optional — WOL just won't work without it

        devices = self.settings.get("devices", [])
        for d in devices:
            if d["ip"] == ip:
                d["name"] = name
                d["mac"] = mac
                save_settings(self.settings)
                return {"status": "ok", "mac": mac}
        devices.append({"name": name, "ip": ip, "mac": mac})
        self.settings["devices"] = devices
        save_settings(self.settings)
        return {"status": "ok", "mac": mac}

    async def remove_device(self, ip: str) -> bool:
        self.settings["devices"] = [
            d for d in self.settings.get("devices", []) if d["ip"] != ip
        ]
        save_settings(self.settings)
        return True

    async def set_tv_ip(self, tv_ip: str) -> bool:
        self.settings["tv_ip"] = tv_ip
        save_settings(self.settings)
        return True

    async def set_tv_mac(self, tv_mac: str) -> bool:
        self.settings["tv_mac"] = tv_mac
        save_settings(self.settings)
        return True

    async def set_tv_client_key(self, key: str) -> bool:
        self.settings["tv_client_key"] = key
        save_settings(self.settings)
        return True

    # ---- Remote commands --------------------------------------------------

    async def wake_device(self, mac: str, ip: str = "") -> dict:
        """Send WOL magic packet to wake a device."""
        try:
            send_wol(mac, host_ip=ip)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def ping_device(self, ip: str) -> dict:
        """Check if the remote plugin is reachable."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, http_get, f"http://{ip}:{PLUGIN_PORT}/ping"
        )
        if result:
            return result
        return {"status": "offline"}

    async def get_remote_status(self, ip: str) -> dict:
        """Get running games from a remote device."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, http_get, f"http://{ip}:{PLUGIN_PORT}/status"
        )
        if result:
            return result
        return {"status": "offline", "games": []}

    async def close_remote_games(self, ip: str) -> dict:
        """Tell a remote device to close all games."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, http_post, f"http://{ip}:{PLUGIN_PORT}/close-all-games"
        )
        if result:
            return result
        return {"status": "error", "message": "Could not reach device"}

    async def shutdown_remote(self, ip: str) -> dict:
        """Tell a remote device to shut down."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, http_post, f"http://{ip}:{PLUGIN_PORT}/shutdown"
        )
        if result:
            return result
        return {"status": "error", "message": "Could not reach device"}

    async def suspend_remote(self, ip: str) -> dict:
        """Tell a remote device to suspend."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None, http_post, f"http://{ip}:{PLUGIN_PORT}/suspend"
        )
        if result:
            return result
        return {"status": "error", "message": "Could not reach device"}

    # ---- Local commands ---------------------------------------------------

    async def get_local_games(self) -> list:
        return get_running_games()

    async def close_local_games(self) -> dict:
        return await close_all_games()

    # ---- LG TV ------------------------------------------------------------

    async def tv_off(self) -> dict:
        """Turn off the configured LG TV."""
        tv_ip = self.settings.get("tv_ip", "")
        if not tv_ip:
            return {"status": "error", "message": "No TV IP configured"}
        client_key = self.settings.get("tv_client_key", "")
        tv_mac = self.settings.get("tv_mac", "")
        result = await turn_off_lg_tv(tv_ip, client_key, tv_mac=tv_mac)
        # Save the client key for future use (skip pairing next time)
        if result.get("client_key") and result["client_key"] != client_key:
            self.settings["tv_client_key"] = result["client_key"]
            save_settings(self.settings)
        return result

    # ---- Full nuke flow ---------------------------------------------------

    async def nuke_device(self, ip: str, mac: str, shutdown_after: bool = True, turn_off_tv: bool = False) -> dict:
        """
        Full workflow with structured step-by-step progress.
        Steps: wake -> boot -> close -> sync -> tv -> sleep
        """
        loop = asyncio.get_event_loop()

        async def step(name: str, status: str, detail: str = ""):
            await decky.emit("nuke_step", name, status, detail)

        try:
            # Step 1: Check if device is already online
            result = await loop.run_in_executor(
                None, http_get, f"http://{ip}:{PLUGIN_PORT}/ping"
            )
            already_online = result and result.get("status") == "ok"

            if already_online:
                await step("wake", "done", "Already online")
                await step("boot", "done", result.get("hostname", ip))
            elif mac:
                # Wake device via WOL
                await step("wake", "active", "Sending wake-on-LAN...")
                send_wol(mac, host_ip=ip)
                await step("wake", "done", "Magic packet sent")

                # Wait for device to boot, resending WOL every 10s
                await step("boot", "active", "Waiting for device...")
                online = False
                since_last_wol = 0
                for attempt in range(60):
                    result = await loop.run_in_executor(
                        None, http_get, f"http://{ip}:{PLUGIN_PORT}/ping"
                    )
                    if result and result.get("status") == "ok":
                        online = True
                        break
                    await asyncio.sleep(2)
                    since_last_wol += 2
                    if since_last_wol >= 10:
                        send_wol(mac, host_ip=ip)
                        since_last_wol = 0

                if not online:
                    await step("boot", "error", "Device did not come online")
                    return {"status": "error", "step": "boot"}
            else:
                await step("wake", "error", "No MAC address — cannot wake device")
                await step("boot", "error", "Device is offline and WOL not available")
                return {"status": "error", "step": "wake"}

            hostname = result.get("hostname", ip)
            await step("boot", "done", f"{hostname} is online")

            # Step 3: Close all games
            await step("close", "active", "Closing all games...")
            result = await loop.run_in_executor(
                None, http_post, f"http://{ip}:{PLUGIN_PORT}/close-all-games"
            )

            if not result:
                await step("close", "error", "Could not reach device")
                return {"status": "error", "step": "close"}

            closed = result.get("closed", [])
            total_pids = result.get("total_pids", 0)
            method = result.get("method", "")
            close_status = result.get("status", "error")

            if close_status == "ok":
                if len(closed) > 0:
                    names = ", ".join(g["name"] for g in closed)
                    await step("close", "done", f"Closed {names} ({total_pids} processes)")
                else:
                    await step("close", "done", "No games were running")
            elif close_status == "partial":
                remaining = result.get("remaining", [])
                rem_names = ", ".join(g["name"] for g in remaining)
                await step("close", "error", f"Could not close: {rem_names}")
                return {"status": "error", "step": "close"}
            else:
                await step("close", "error", "Failed to close games")
                return {"status": "error", "step": "close"}

            # Step 4: Wait for cloud sync
            await step("sync", "active", "Waiting for Steam Cloud sync...")
            await asyncio.sleep(10)
            await step("sync", "done", "Cloud save sync complete")

            # Step 5: Turn off TV (if configured)
            if turn_off_tv:
                await step("tv", "active", "Turning off TV...")
                tv_result = await self.tv_off()
                if tv_result.get("status") == "ok":
                    await step("tv", "done", "TV is off")
                else:
                    await step("tv", "error", tv_result.get("message", "Failed"))

            # Step 6: Put device back to sleep
            if shutdown_after:
                await step("sleep", "active", "Putting device to sleep...")
                await loop.run_in_executor(
                    None, http_post, f"http://{ip}:{PLUGIN_PORT}/suspend"
                )
                await step("sleep", "done", "Device is sleeping")

            await step("finished", "done", "All done!")
            return {"status": "ok", "closed": len(closed)}

        except Exception as e:
            decky.logger.error(f"Nuke error: {e}")
            await step("error", "error", str(e))
            return {"status": "error", "message": str(e)}
