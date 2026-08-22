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
    """Find running Steam game processes by scanning /proc for SteamAppId env var."""
    games = []
    seen_apps = set()
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                env_path = f"/proc/{entry}/environ"
                with open(env_path, "rb") as f:
                    environ = f.read()
                if b"SteamAppId=" not in environ:
                    continue
                app_id = None
                for var in environ.split(b"\x00"):
                    if var.startswith(b"SteamAppId="):
                        app_id = var.split(b"=", 1)[1].decode()
                        break
                if app_id and app_id != "0" and app_id not in seen_apps:
                    # Try to get process name
                    name = "unknown"
                    try:
                        with open(f"/proc/{entry}/comm", "r") as f:
                            name = f.read().strip()
                    except Exception:
                        pass
                    seen_apps.add(app_id)
                    games.append({
                        "pid": int(entry),
                        "app_id": app_id,
                        "name": name,
                    })
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception:
        pass
    return games


def close_all_games() -> dict:
    """Send SIGTERM to all running Steam game processes."""
    games = get_running_games()
    closed = []
    for game in games:
        try:
            os.kill(game["pid"], signal.SIGTERM)
            closed.append(game)
            decky.logger.info(f"Sent SIGTERM to {game['name']} (AppId={game['app_id']}, PID={game['pid']})")
        except ProcessLookupError:
            pass
        except Exception as e:
            decky.logger.error(f"Failed to kill PID {game['pid']}: {e}")
    return {"status": "ok", "closed": closed}


async def wait_for_games_to_exit(timeout: int = 30) -> bool:
    """Wait until no game processes remain, up to timeout seconds."""
    for _ in range(timeout):
        if not get_running_games():
            return True
        await asyncio.sleep(1)
    return False


def http_get(url: str, timeout: int = 5) -> dict | None:
    """Synchronous HTTP GET, returns parsed JSON or None."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def http_post(url: str, data: dict | None = None, timeout: int = 10) -> dict | None:
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


async def turn_off_lg_tv(tv_ip: str, client_key: str = "") -> dict:
    """Turn off an LG WebOS TV using the SSAP protocol over raw WebSocket."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(tv_ip, 3000), timeout=5
        )

        # WebSocket handshake
        ws_key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET / HTTP/1.1\r\n"
            f"Host: {tv_ip}:3000\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {ws_key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        writer.write(handshake.encode())
        await writer.drain()

        # Read handshake response
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        if b"101" not in response:
            writer.close()
            return {"status": "error", "message": "WebSocket handshake failed"}

        # Helper: send WebSocket text frame
        async def ws_send(data: str):
            payload = data.encode()
            frame = bytearray()
            frame.append(0x81)  # FIN + text
            length = len(payload)
            mask_key = os.urandom(4)
            if length < 126:
                frame.append(0x80 | length)  # masked
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

        # Helper: receive WebSocket text frame
        async def ws_recv() -> str:
            header = await asyncio.wait_for(reader.read(2), timeout=10)
            if len(header) < 2:
                return ""
            length = header[1] & 0x7F
            if length == 126:
                ext = await reader.read(2)
                length = struct.unpack(">H", ext)[0]
            elif length == 127:
                ext = await reader.read(8)
                length = struct.unpack(">Q", ext)[0]
            data = await asyncio.wait_for(reader.read(length), timeout=10)
            return data.decode()

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

        # Wait for registration response (may need TV prompt acceptance)
        for _ in range(60):  # 60 second timeout for user to accept on TV
            msg = await ws_recv()
            if not msg:
                continue
            data = json.loads(msg)
            if data.get("type") == "registered":
                new_key = data.get("payload", {}).get("client-key", client_key)
                # Send power off
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
        return {"status": "error", "message": "Registration timed out (accept on TV?)"}
    except Exception as e:
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
            result = close_all_games()
            # Wait for games to actually exit so cloud sync can happen
            synced = await wait_for_games_to_exit(timeout=30)
            result["cloud_sync_waited"] = synced
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
        # Auto-resolve MAC if not provided
        if not mac:
            resolved = await loop.run_in_executor(None, resolve_mac, ip)
            if resolved:
                mac = resolved
                decky.logger.info(f"Auto-resolved MAC for {ip}: {mac}")
            else:
                return {"status": "error", "message": f"Could not resolve MAC for {ip}. Is the device online?"}

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

    async def resolve_device_mac(self, ip: str) -> dict:
        """Try to resolve MAC address for an IP."""
        loop = asyncio.get_event_loop()
        mac = await loop.run_in_executor(None, resolve_mac, ip)
        if mac:
            return {"status": "ok", "mac": mac}
        return {"status": "error", "message": "Could not resolve MAC. Is the device online?"}

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
        result = close_all_games()
        synced = await wait_for_games_to_exit(timeout=30)
        result["cloud_sync_waited"] = synced
        return result

    # ---- LG TV ------------------------------------------------------------

    async def tv_off(self) -> dict:
        """Turn off the configured LG TV."""
        tv_ip = self.settings.get("tv_ip", "")
        if not tv_ip:
            return {"status": "error", "message": "No TV IP configured"}
        client_key = self.settings.get("tv_client_key", "")
        result = await turn_off_lg_tv(tv_ip, client_key)
        # Save the client key for future use (skip pairing next time)
        if result.get("client_key") and result["client_key"] != client_key:
            self.settings["tv_client_key"] = result["client_key"]
            save_settings(self.settings)
        return result

    # ---- Full nuke flow ---------------------------------------------------

    async def nuke_device(self, ip: str, mac: str, shutdown_after: bool = True, turn_off_tv: bool = False) -> dict:
        """
        Full workflow: wake device -> wait for plugin -> close games -> shutdown -> TV off.
        Emits progress events to the frontend.
        """
        try:
            await decky.emit("nuke_progress", "Sending wake-on-LAN...")
            send_wol(mac, host_ip=ip)

            # Wait for the remote plugin to come online, resending WOL every 10s
            await decky.emit("nuke_progress", "Waiting for device to boot...")
            loop = asyncio.get_event_loop()
            online = False
            since_last_wol = 0
            for attempt in range(60):  # up to 120 seconds (60 * 2s sleep)
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
                    decky.logger.info("Resending WOL packet...")

            if not online:
                await decky.emit("nuke_progress", "ERROR: Device did not come online")
                return {"status": "error", "message": "Device did not come online after WOL"}

            # Close all games
            await decky.emit("nuke_progress", "Closing all games...")
            result = await loop.run_in_executor(
                None, http_post, f"http://{ip}:{PLUGIN_PORT}/close-all-games"
            )
            if not result or result.get("status") != "ok":
                await decky.emit("nuke_progress", "ERROR: Failed to close games")
                return {"status": "error", "message": "Failed to close games on remote device"}

            closed_count = len(result.get("closed", []))
            await decky.emit("nuke_progress", f"Closed {closed_count} game(s). Cloud syncing...")

            # Give extra time for cloud sync
            await asyncio.sleep(5)

            # Shutdown if requested
            if shutdown_after:
                await decky.emit("nuke_progress", "Shutting down device...")
                await loop.run_in_executor(
                    None, http_post, f"http://{ip}:{PLUGIN_PORT}/shutdown"
                )

            # Turn off TV if requested
            if turn_off_tv:
                await decky.emit("nuke_progress", "Turning off TV...")
                tv_result = await self.tv_off()
                if tv_result.get("status") != "ok":
                    await decky.emit("nuke_progress", f"TV: {tv_result.get('message', 'unknown error')}")

            await decky.emit("nuke_progress", "Done!")
            return {"status": "ok", "closed": closed_count}

        except Exception as e:
            await decky.emit("nuke_progress", f"ERROR: {str(e)}")
            return {"status": "error", "message": str(e)}
