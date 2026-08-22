import { useState, useEffect, useCallback, Fragment } from "react";
import {
  definePlugin,
  callable,
  addEventListener,
  removeEventListener,
  toaster,
} from "@decky/api";
import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  TextField,
  staticClasses,
  Focusable,
  DialogButton,
} from "@decky/ui";
import { FaPowerOff, FaPlus, FaTrash, FaTv, FaDesktop, FaSyncAlt } from "react-icons/fa";

// Backend callables
const getSettings = callable<[], Settings>("get_settings");
const addDevice = callable<[name: string, ip: string, mac: string], boolean>("add_device");
const removeDevice = callable<[ip: string], boolean>("remove_device");
const setTvIp = callable<[tvIp: string], boolean>("set_tv_ip");
const pingDevice = callable<[ip: string], PingResult>("ping_device");
const wakeDevice = callable<[mac: string], StatusResult>("wake_device");
const getRemoteStatus = callable<[ip: string], RemoteStatus>("get_remote_status");
const closeRemoteGames = callable<[ip: string], CloseResult>("close_remote_games");
const nukeDevice = callable<
  [ip: string, mac: string, shutdownAfter: boolean, turnOffTv: boolean],
  NukeResult
>("nuke_device");
const tvOff = callable<[], StatusResult>("tv_off");
const getLocalGames = callable<[], GameInfo[]>("get_local_games");
const closeLocalGames = callable<[], CloseResult>("close_local_games");

// Types
interface Device {
  name: string;
  ip: string;
  mac: string;
}

interface Settings {
  devices: Device[];
  tv_ip: string;
  tv_client_key?: string;
  plugin_port?: number;
}

interface StatusResult {
  status: string;
  message?: string;
}

interface PingResult {
  status: string;
  hostname?: string;
}

interface GameInfo {
  pid: number;
  app_id: string;
  name: string;
}

interface RemoteStatus {
  status: string;
  games: GameInfo[];
  hostname?: string;
}

interface CloseResult {
  status: string;
  closed: GameInfo[];
  cloud_sync_waited?: boolean;
}

interface NukeResult {
  status: string;
  closed?: number;
  message?: string;
}

// ---------------------------------------------------------------------------
// Add Device form (shown inline)
// ---------------------------------------------------------------------------
function AddDeviceForm({ onAdd, onCancel }: { onAdd: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [ip, setIp] = useState("");
  const [mac, setMac] = useState("");

  const handleAdd = async () => {
    if (!name || !ip || !mac) {
      toaster.toast({ title: "Missing fields", body: "Please fill in all fields" });
      return;
    }
    await addDevice(name, ip, mac);
    toaster.toast({ title: "Device added", body: `${name} (${ip})` });
    onAdd();
  };

  return (
    <PanelSection title="Add Device">
      <PanelSectionRow>
        <TextField label="Name" value={name} onChange={(e) => setName(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label="IP Address" value={ip} onChange={(e) => setIp(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <TextField label="MAC Address" value={mac} onChange={(e) => setMac(e.target.value)} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={handleAdd}>
          Save Device
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={onCancel}>
          Cancel
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

// ---------------------------------------------------------------------------
// Device card
// ---------------------------------------------------------------------------
function DeviceCard({
  device,
  onRemove,
  tvIp,
}: {
  device: Device;
  onRemove: () => void;
  tvIp: string;
}) {
  const [status, setStatus] = useState<string>("unknown");
  const [games, setGames] = useState<GameInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState("");

  const refreshStatus = useCallback(async () => {
    const result = await pingDevice(device.ip);
    if (result.status === "ok") {
      setStatus("online");
      const remote = await getRemoteStatus(device.ip);
      setGames(remote.games || []);
    } else {
      setStatus("offline");
      setGames([]);
    }
  }, [device.ip]);

  useEffect(() => {
    refreshStatus();
  }, [refreshStatus]);

  // Listen for nuke progress events
  useEffect(() => {
    const listener = addEventListener<[message: string]>("nuke_progress", (msg) => {
      setProgress(msg);
      if (msg.startsWith("Done") || msg.startsWith("ERROR")) {
        setBusy(false);
        refreshStatus();
      }
    });
    return () => {
      removeEventListener("nuke_progress", listener);
    };
  }, [refreshStatus]);

  const handleNuke = async () => {
    setBusy(true);
    setProgress("Starting...");
    const hasTv = tvIp !== "";
    await nukeDevice(device.ip, device.mac, true, hasTv);
  };

  const handleCloseOnly = async () => {
    setBusy(true);
    setProgress("Closing games...");
    if (status === "offline") {
      setProgress("Waking device...");
      await wakeDevice(device.mac);
      // Wait for device
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const ping = await pingDevice(device.ip);
        if (ping.status === "ok") break;
      }
    }
    const result = await closeRemoteGames(device.ip);
    if (result.status === "ok") {
      const count = result.closed?.length || 0;
      setProgress(`Closed ${count} game(s)`);
      toaster.toast({ title: "Games closed", body: `${count} game(s) on ${device.name}` });
    } else {
      setProgress("Failed to close games");
    }
    setBusy(false);
    refreshStatus();
  };

  const statusIcon = status === "online" ? "🟢" : status === "offline" ? "🔴" : "⚪";

  return (
    <PanelSection title={`${statusIcon} ${device.name}`}>
      <PanelSectionRow>
        <div style={{ fontSize: "12px", color: "#8b929a", marginBottom: "4px" }}>
          {device.ip} | {device.mac}
        </div>
      </PanelSectionRow>

      {games.length > 0 && (
        <PanelSectionRow>
          <div style={{ fontSize: "12px", color: "#dca", marginBottom: "4px" }}>
            Running: {games.map((g) => g.name).join(", ")}
          </div>
        </PanelSectionRow>
      )}

      {busy && progress && (
        <PanelSectionRow>
          <div style={{ fontSize: "12px", color: "#6bf", marginBottom: "4px" }}>
            {progress}
          </div>
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={handleNuke}>
          <FaPowerOff style={{ marginRight: "8px" }} />
          Nuke (Close + Shutdown + TV Off)
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={handleCloseOnly}>
          <FaSyncAlt style={{ marginRight: "8px" }} />
          Close Games Only
        </ButtonItem>
      </PanelSectionRow>

      <PanelSectionRow>
        <Focusable style={{ display: "flex", gap: "8px" }}>
          <DialogButton
            style={{ flex: 1, minWidth: 0 }}
            disabled={busy}
            onClick={refreshStatus}
          >
            Refresh
          </DialogButton>
          <DialogButton
            style={{ flex: 1, minWidth: 0 }}
            disabled={busy}
            onClick={onRemove}
          >
            <FaTrash />
          </DialogButton>
        </Focusable>
      </PanelSectionRow>
    </PanelSection>
  );
}

// ---------------------------------------------------------------------------
// Main panel
// ---------------------------------------------------------------------------
function MainPanel() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [showAddForm, setShowAddForm] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [tvIpInput, setTvIpInput] = useState("");
  const [localGames, setLocalGames] = useState<GameInfo[]>([]);
  const [closingLocal, setClosingLocal] = useState(false);

  const loadSettings = useCallback(async () => {
    const s = await getSettings();
    setSettings(s);
    setTvIpInput(s.tv_ip || "");
  }, []);

  const loadLocalGames = useCallback(async () => {
    const games = await getLocalGames();
    setLocalGames(games);
  }, []);

  useEffect(() => {
    loadSettings();
    loadLocalGames();
  }, [loadSettings, loadLocalGames]);

  const handleRemoveDevice = async (ip: string) => {
    await removeDevice(ip);
    await loadSettings();
  };

  const handleCloseLocal = async () => {
    setClosingLocal(true);
    const result = await closeLocalGames();
    const count = result.closed?.length || 0;
    toaster.toast({ title: "Local games closed", body: `Closed ${count} game(s)` });
    setClosingLocal(false);
    loadLocalGames();
  };

  const handleSaveTvIp = async () => {
    await setTvIp(tvIpInput);
    toaster.toast({ title: "TV IP saved", body: tvIpInput || "(cleared)" });
    await loadSettings();
  };

  const handleTvOff = async () => {
    const result = await tvOff();
    if (result.status === "ok") {
      toaster.toast({ title: "TV", body: "Turning off..." });
    } else {
      toaster.toast({ title: "TV Error", body: result.message || "Failed" });
    }
  };

  if (!settings) {
    return (
      <PanelSection title="Loading...">
        <PanelSectionRow>
          <div>Loading settings...</div>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  return (
    <Fragment>
      {/* Local games section */}
      {localGames.length > 0 && (
        <PanelSection title="This Device">
          <PanelSectionRow>
            <div style={{ fontSize: "12px", color: "#dca" }}>
              Running: {localGames.map((g) => g.name).join(", ")}
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem layout="below" disabled={closingLocal} onClick={handleCloseLocal}>
              Close Local Games
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Remote devices */}
      {settings.devices.map((device) => (
        <DeviceCard
          key={device.ip}
          device={device}
          tvIp={settings.tv_ip || ""}
          onRemove={() => handleRemoveDevice(device.ip)}
        />
      ))}

      {/* Add device */}
      {showAddForm ? (
        <AddDeviceForm
          onAdd={() => {
            setShowAddForm(false);
            loadSettings();
          }}
          onCancel={() => setShowAddForm(false)}
        />
      ) : (
        <PanelSection>
          <PanelSectionRow>
            <ButtonItem layout="below" onClick={() => setShowAddForm(true)}>
              <FaPlus style={{ marginRight: "8px" }} />
              Add Remote Device
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* Settings toggle */}
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setShowSettings(!showSettings)}>
            {showSettings ? "Hide Settings" : "Settings"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {/* Settings panel */}
      {showSettings && (
        <PanelSection title="LG TV">
          <PanelSectionRow>
            <TextField
              label="TV IP Address"
              value={tvIpInput}
              onChange={(e) => setTvIpInput(e.target.value)}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <Focusable style={{ display: "flex", gap: "8px" }}>
              <DialogButton style={{ flex: 1, minWidth: 0 }} onClick={handleSaveTvIp}>
                Save
              </DialogButton>
              <DialogButton
                style={{ flex: 1, minWidth: 0 }}
                disabled={!settings.tv_ip}
                onClick={handleTvOff}
              >
                <FaTv style={{ marginRight: "4px" }} />
                TV Off
              </DialogButton>
            </Focusable>
          </PanelSectionRow>
        </PanelSection>
      )}
    </Fragment>
  );
}

// ---------------------------------------------------------------------------
// Plugin entry
// ---------------------------------------------------------------------------
export default definePlugin(() => {
  return {
    name: "Close Games Remotely",
    titleView: <div className={staticClasses.Title}>Close Games Remotely</div>,
    content: <MainPanel />,
    icon: <FaDesktop />,
    onDismount() {},
  };
});
