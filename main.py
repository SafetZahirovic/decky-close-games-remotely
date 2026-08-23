import os
import json
import asyncio
import signal
import socket
import urllib.request
import decky

PLUGIN_PORT = 55123
SETTINGS_FILE = "settings.json"


def load_settings():
    path = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, SETTINGS_FILE)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {"devices": [], "plugin_port": PLUGIN_PORT}


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

            content_length = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=10)
                if line in (b"\r\n", b"\n", b""):
                    break
                header = line.decode().strip()
                if header.lower().startswith("content-length:"):
                    content_length = int(header.split(":", 1)[1].strip())

            body = None
            if content_length > 0:
                body = await asyncio.wait_for(reader.read(content_length), timeout=10)

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
def get_local_mac() -> str:
    """Get the MAC address of the default network interface."""
    import subprocess
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"], capture_output=True, text=True, timeout=5
        )
        parts = result.stdout.split()
        if "dev" in parts:
            iface = parts[parts.index("dev") + 1]
            with open(f"/sys/class/net/{iface}/address", "r") as f:
                return f.read().strip()
    except Exception:
        pass
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


def _get_subnet_broadcast(target_ip: str) -> str | None:
    """Compute the subnet broadcast address for a target IP."""
    import ipaddress
    import subprocess
    try:
        target = ipaddress.IPv4Address(target_ip)
        result = subprocess.run(
            ["ip", "-4", "addr", "show"], capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line.startswith("inet "):
                parts = line.split()
                try:
                    network = ipaddress.IPv4Network(parts[1], strict=False)
                    if target in network:
                        return str(network.broadcast_address)
                except (ValueError, IndexError):
                    continue
    except Exception as e:
        decky.logger.warning(f"Could not compute subnet broadcast: {e}")
    return None


def send_wol(mac_address: str, host_ip: str = "", port: int = 9):
    """Send Wake-on-LAN magic packets to 3 targets:
    1. Subnet broadcast — most reliable
    2. Direct to host IP
    3. Global broadcast (255.255.255.255) — fallback
    """
    mac_bytes = bytes.fromhex(mac_address.replace(":", "").replace("-", ""))
    magic = b"\xff" * 6 + mac_bytes * 16

    seen = set()
    targets = []

    def add_target(addr: str, family: int = socket.AF_INET):
        key = (addr, family)
        if key not in seen:
            seen.add(key)
            targets.append(key)

    if host_ip:
        subnet_bcast = _get_subnet_broadcast(host_ip)
        if subnet_bcast:
            add_target(subnet_bcast)
        try:
            for info in socket.getaddrinfo(host_ip, port, family=socket.AF_UNSPEC):
                family, _, _, _, sockaddr = info
                if family in (socket.AF_INET, socket.AF_INET6):
                    add_target(sockaddr[0], family)
        except socket.gaierror:
            add_target(host_ip)

    add_target("255.255.255.255")

    for addr, family in targets:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.connect((addr, port))
                sock.send(magic)
                decky.logger.info(f"WOL packet sent to {addr}:{port}")
        except OSError as err:
            if err.errno in (101, 65, 113):
                decky.logger.warning(f"WOL to {addr} failed: {err}")
            else:
                raise


def get_running_games() -> list[dict]:
    """Find ALL running Steam game processes, grouped by app ID."""
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
    for app in apps.values():
        app["pgids"] = list(app["pgids"])
    return list(apps.values())


def _kill_games(games: list[dict], sig: int):
    """Kill games by process group first, then individual PIDs as fallback."""
    sig_name = signal.Signals(sig).name
    killed_pgids: set[int] = set()

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

    for game in games:
        for pid in game["pids"]:
            try:
                os.kill(pid, sig)
                decky.logger.info(f"Sent {sig_name} to PID {pid} ({game['name']})")
            except ProcessLookupError:
                pass
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

    _kill_games(initial_games, signal.SIGTERM)
    for _ in range(10):
        await asyncio.sleep(1)
        if not get_running_games():
            decky.logger.info("All games exited after SIGTERM")
            return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGTERM"}

    respawned = get_running_games()
    if respawned:
        decky.logger.warning("Games still running after first SIGTERM, re-scanning...")
        _kill_games(respawned, signal.SIGTERM)
        for _ in range(5):
            await asyncio.sleep(1)
            if not get_running_games():
                return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGTERM"}

    remaining = get_running_games()
    if remaining:
        decky.logger.warning(f"SIGKILL {len(remaining)} game(s)")
        _kill_games(remaining, signal.SIGKILL)
        for _ in range(5):
            await asyncio.sleep(1)
            if not get_running_games():
                return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGKILL"}

    still = get_running_games()
    if still:
        return {"status": "partial", "closed": initial_games, "remaining": still, "total_pids": total_pids}
    return {"status": "ok", "closed": initial_games, "total_pids": total_pids, "method": "SIGKILL"}


def http_get(url: str, timeout: int = 5) -> dict | None:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def http_post(url: str, data: dict | None = None, timeout: int = 60) -> dict | None:
    try:
        body = json.dumps(data or {}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None



# ---------------------------------------------------------------------------
# HDMI-CEC TV control (via cec-ctl)
# ---------------------------------------------------------------------------
def cec_standby() -> dict:
    """Turn off the TV via HDMI-CEC standby command."""
    import subprocess
    clean_env = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "HOME": "/root"}
    for cmd in [
        ["cec-ctl", "--standby", "-t0"],
        ["cec-ctl", "-d/dev/cec0", "--standby", "-t0"],
        ["cec-ctl", "--standby"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=clean_env)
            decky.logger.info(f"CEC {cmd}: rc={result.returncode}, out={result.stdout.strip()}, err={result.stderr.strip()}")
            if result.returncode == 0:
                return {"status": "ok", "method": "cec", "command": " ".join(cmd)}
        except FileNotFoundError:
            continue
        except Exception as e:
            decky.logger.error(f"CEC {cmd} failed: {e}")
            continue
    return {"status": "error", "message": "cec-ctl not found or CEC commands failed"}


def cec_wakeup() -> dict:
    """Turn on the TV via HDMI-CEC image-view-on command."""
    import subprocess
    clean_env = {"PATH": "/usr/bin:/usr/sbin:/bin:/sbin", "HOME": "/root"}
    for cmd in [
        ["cec-ctl", "--image-view-on", "-t0"],
        ["cec-ctl", "-d/dev/cec0", "--image-view-on", "-t0"],
        ["cec-ctl", "--image-view-on"],
    ]:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=clean_env)
            decky.logger.info(f"CEC wake {cmd}: rc={result.returncode}, out={result.stdout.strip()}, err={result.stderr.strip()}")
            if result.returncode == 0:
                return {"status": "ok", "method": "cec"}
        except FileNotFoundError:
            continue
        except Exception as e:
            decky.logger.error(f"CEC wake {cmd} failed: {e}")
            continue
    return {"status": "error", "message": "cec-ctl not found or CEC commands failed"}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------
class Plugin:
    http_server: MiniHTTPServer | None = None
    settings: dict = {}

    async def _main(self):
        self.settings = load_settings()
        port = self.settings.get("plugin_port", PLUGIN_PORT)

        self.http_server = MiniHTTPServer(port=port)

        @self.http_server.route("GET", "/ping")
        async def handle_ping(_body):
            mac = get_local_mac()
            return {"status": "ok", "hostname": socket.gethostname(), "mac": mac}

        @self.http_server.route("GET", "/status")
        async def handle_status(_body):
            games = get_running_games()
            return {"status": "ok", "games": games, "hostname": socket.gethostname()}

        @self.http_server.route("POST", "/close-all-games")
        async def handle_close_games(_body):
            return await close_all_games()

        @self.http_server.route("POST", "/shutdown")
        async def handle_shutdown(_body):
            asyncio.get_event_loop().call_later(3, lambda: os.system("systemctl poweroff"))
            return {"status": "shutting_down"}

        @self.http_server.route("POST", "/suspend")
        async def handle_suspend(_body):
            # Emit event to frontend — frontend calls Steam's own suspend API
            await decky.emit("do_suspend")
            return {"status": "suspending", "method": "steam-api"}

        @self.http_server.route("POST", "/cec-standby")
        async def handle_cec_standby(_body):
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, cec_standby)

        @self.http_server.route("POST", "/cec-wakeup")
        async def handle_cec_wakeup(_body):
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, cec_wakeup)

        await self.http_server.start()
        decky.logger.info("Close Games Remotely plugin loaded")

    async def _unload(self):
        if self.http_server:
            await self.http_server.stop()
        save_settings(self.settings)

    # ---- Settings ---------------------------------------------------------

    async def get_settings(self) -> dict:
        return self.settings

    async def add_device(self, name: str, ip: str, mac: str = "") -> dict:
        loop = asyncio.get_event_loop()
        if not mac:
            ping_result = await loop.run_in_executor(
                None, http_get, f"http://{ip}:{PLUGIN_PORT}/ping"
            )
            if ping_result and ping_result.get("mac"):
                mac = ping_result["mac"]
                decky.logger.info(f"Got MAC from remote plugin for {ip}: {mac}")

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

    # ---- Remote commands --------------------------------------------------

    async def wake_device(self, mac: str, ip: str = "") -> dict:
        try:
            send_wol(mac, host_ip=ip)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def ping_device(self, ip: str) -> dict:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, http_get, f"http://{ip}:{PLUGIN_PORT}/ping")
        return result if result else {"status": "offline"}

    async def get_remote_status(self, ip: str) -> dict:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, http_get, f"http://{ip}:{PLUGIN_PORT}/status")
        return result if result else {"status": "offline", "games": []}

    async def close_remote_games(self, ip: str) -> dict:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, http_post, f"http://{ip}:{PLUGIN_PORT}/close-all-games")
        return result if result else {"status": "error", "message": "Could not reach device"}

    async def suspend_remote(self, ip: str) -> dict:
        loop = asyncio.get_event_loop()
        # Use a shorter timeout — if suspend works, the connection drops
        result = await loop.run_in_executor(
            None, lambda: http_post(f"http://{ip}:{PLUGIN_PORT}/suspend", timeout=15)
        )
        if result:
            return result
        # Timeout/connection drop likely means suspend worked
        return {"status": "suspending", "method": "connection lost (likely suspended)"}

    async def cec_tv_off_remote(self, ip: str) -> dict:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, http_post, f"http://{ip}:{PLUGIN_PORT}/cec-standby")
        return result if result else {"status": "error", "message": "Could not reach device"}

    async def cec_tv_on_remote(self, ip: str) -> dict:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, http_post, f"http://{ip}:{PLUGIN_PORT}/cec-wakeup")
        return result if result else {"status": "error", "message": "Could not reach device"}

    # ---- Local commands ---------------------------------------------------

    async def get_local_games(self) -> list:
        return get_running_games()

    async def close_local_games(self) -> dict:
        return await close_all_games()

    async def cec_tv_off_local(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, cec_standby)

    # ---- Full nuke flow ---------------------------------------------------

    async def nuke_device(self, ip: str, mac: str, shutdown_after: bool = True, turn_off_tv: bool = False) -> dict:
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
                await step("wake", "active", "Sending wake-on-LAN...")
                send_wol(mac, host_ip=ip)
                await step("wake", "done", "Magic packet sent")

                await step("boot", "active", "Waiting for device...")
                online = False
                since_last_wol = 0
                for _ in range(60):
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
                await step("wake", "error", "No MAC — cannot wake device")
                return {"status": "error", "step": "wake"}

            hostname = result.get("hostname", ip)
            await step("boot", "done", f"{hostname} is online")

            # Step 2: Close all games
            await step("close", "active", "Closing all games...")
            result = await loop.run_in_executor(
                None, http_post, f"http://{ip}:{PLUGIN_PORT}/close-all-games"
            )

            if not result:
                await step("close", "error", "Could not reach device")
                return {"status": "error", "step": "close"}

            closed = result.get("closed", [])
            total_pids = result.get("total_pids", 0)
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

            # Step 3: Wait for cloud sync
            await step("sync", "active", "Waiting for Steam Cloud sync...")
            await asyncio.sleep(10)
            await step("sync", "done", "Cloud save sync complete")

            # Step 4: Turn off TV via CEC
            if turn_off_tv:
                await step("tv", "active", "Turning off TV via CEC...")
                tv_result = await self.cec_tv_off_remote(ip)
                if tv_result.get("status") == "ok":
                    await step("tv", "done", "TV off")
                else:
                    await step("tv", "error", tv_result.get("message", "CEC failed"))

            # Step 5: Put device to sleep
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
