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
const addDevice = callable<[name: string, ip: string, mac: string], AddDeviceResult>("add_device");
const removeDevice = callable<[ip: string], boolean>("remove_device");
const setTvIp = callable<[tvIp: string], boolean>("set_tv_ip");
const pingDevice = callable<[ip: string], PingResult>("ping_device");
const wakeDevice = callable<[mac: string, ip: string], StatusResult>("wake_device");
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
interface Device { name: string; ip: string; mac: string }
interface Settings { devices: Device[]; tv_ip: string; tv_client_key?: string; plugin_port?: number }
interface StatusResult { status: string; message?: string }
interface PingResult { status: string; hostname?: string; mac?: string }
interface AddDeviceResult { status: string; mac?: string; message?: string }
interface GameInfo { pid: number; app_id: string; name: string; pids?: number[] }
interface RemoteStatus { status: string; games: GameInfo[]; hostname?: string }
interface CloseResult { status: string; closed: GameInfo[]; total_pids?: number; method?: string }
interface NukeResult { status: string; closed?: number; message?: string; step?: string }

// Step tracking for nuke progress
interface StepState { status: "pending" | "active" | "done" | "error"; detail: string }

const STEP_LABELS: Record<string, string> = {
  wake: "Waking device",
  boot: "Device booting",
  close: "Closing games",
  sync: "Cloud save sync",
  tv: "Turning off TV",
  sleep: "Putting to sleep",
};

const STEP_ICONS: Record<string, string> = {
  pending: "\u2022",  // bullet
  active: "\u23F3",   // hourglass
  done: "\u2705",     // checkmark
  error: "\u274C",    // cross
};

function initialSteps(hasTv: boolean): Record<string, StepState> {
  const steps: Record<string, StepState> = {
    wake: { status: "pending", detail: "" },
    boot: { status: "pending", detail: "" },
    close: { status: "pending", detail: "" },
    sync: { status: "pending", detail: "" },
  };
  if (hasTv) steps.tv = { status: "pending", detail: "" };
  steps.sleep = { status: "pending", detail: "" };
  return steps;
}

// ---------------------------------------------------------------------------
// Step progress display
// ---------------------------------------------------------------------------
function StepProgress({ steps }: { steps: Record<string, StepState> }) {
  return (
    <div style={{ padding: "4px 0" }}>
      {Object.entries(steps).map(([key, step]) => {
        const label = STEP_LABELS[key] || key;
        const icon = STEP_ICONS[step.status];
        const color =
          step.status === "active" ? "#6bf" :
          step.status === "done" ? "#6b6" :
          step.status === "error" ? "#f66" :
          "#666";
        return (
          <div key={key} style={{ fontSize: "12px", color, padding: "2px 0" }}>
            <span style={{ marginRight: "6px" }}>{icon}</span>
            <span style={{ fontWeight: step.status === "active" ? "bold" : "normal" }}>
              {label}
            </span>
            {step.detail && (
              <span style={{ color: "#8b929a", marginLeft: "6px" }}>
                — {step.detail}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Add Device form
// ---------------------------------------------------------------------------
function AddDeviceForm({ onAdd, onCancel }: { onAdd: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [ip, setIp] = useState("");
  const [mac, setMac] = useState("");
  const [adding, setAdding] = useState(false);

  const handleAdd = async () => {
    if (!name || !ip) {
      toaster.toast({ title: "Missing fields", body: "Name and IP are required" });
      return;
    }
    setAdding(true);
    const result = await addDevice(name, ip, mac);
    setAdding(false);
    if (result.status === "ok") {
      toaster.toast({ title: "Device added", body: `${name} (${ip}) - MAC: ${result.mac}` });
      onAdd();
    } else {
      toaster.toast({ title: "Error", body: result.message || "Failed to add device" });
    }
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
        <TextField
          label="MAC Address (auto-detected if empty)"
          value={mac}
          onChange={(e) => setMac(e.target.value)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={adding} onClick={handleAdd}>
          {adding ? "Detecting MAC..." : "Save Device"}
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
  const [steps, setSteps] = useState<Record<string, StepState> | null>(null);

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

  // Listen for structured step events
  useEffect(() => {
    const listener = addEventListener<[name: string, status: string, detail: string]>(
      "nuke_step",
      (stepName, stepStatus, detail) => {
        setSteps((prev) => {
          if (!prev) return prev;
          if (stepName === "finished" || stepName === "error") {
            setBusy(false);
            refreshStatus();
            // Keep steps visible for a moment, then clear
            setTimeout(() => setSteps(null), 5000);
            return prev;
          }
          return {
            ...prev,
            [stepName]: { status: stepStatus as StepState["status"], detail },
          };
        });
      }
    );
    return () => {
      removeEventListener("nuke_step", listener);
    };
  }, [refreshStatus]);

  const handleNuke = async () => {
    const hasTv = tvIp !== "";
    setBusy(true);
    setSteps(initialSteps(hasTv));
    await nukeDevice(device.ip, device.mac, true, hasTv);
  };

  const handleCloseOnly = async () => {
    setBusy(true);
    setSteps({
      wake: { status: "pending", detail: "" },
      boot: { status: "pending", detail: "" },
      close: { status: "pending", detail: "" },
    });

    if (status === "offline") {
      setSteps((prev) => prev && { ...prev, wake: { status: "active", detail: "Sending WOL..." } });
      await wakeDevice(device.mac, device.ip);
      setSteps((prev) => prev && { ...prev, wake: { status: "done", detail: "" }, boot: { status: "active", detail: "Waiting..." } });
      for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 2000));
        const ping = await pingDevice(device.ip);
        if (ping.status === "ok") break;
      }
      setSteps((prev) => prev && { ...prev, boot: { status: "done", detail: "" } });
    } else {
      setSteps((prev) => prev && {
        ...prev,
        wake: { status: "done", detail: "Already online" },
        boot: { status: "done", detail: "" },
      });
    }

    setSteps((prev) => prev && { ...prev, close: { status: "active", detail: "Closing games..." } });
    const result = await closeRemoteGames(device.ip);
    if (result.status === "ok") {
      const count = result.closed?.length || 0;
      const names = result.closed?.map((g) => g.name).join(", ") || "";
      setSteps((prev) => prev && { ...prev, close: { status: "done", detail: count > 0 ? `Closed ${names}` : "No games running" } });
      toaster.toast({ title: "Games closed", body: `${count} game(s) on ${device.name}` });
    } else {
      setSteps((prev) => prev && { ...prev, close: { status: "error", detail: "Failed to close games" } });
    }
    setBusy(false);
    refreshStatus();
    setTimeout(() => setSteps(null), 5000);
  };

  const statusIcon = status === "online" ? "\uD83D\uDFE2" : status === "offline" ? "\uD83D\uDD34" : "\u26AA";

  return (
    <PanelSection title={`${statusIcon} ${device.name}`}>
      <PanelSectionRow>
        <div style={{ fontSize: "12px", color: "#8b929a", marginBottom: "4px" }}>
          {device.ip} | {device.mac}
        </div>
      </PanelSectionRow>

      {games.length > 0 && !busy && (
        <PanelSectionRow>
          <div style={{ fontSize: "12px", color: "#dca", marginBottom: "4px" }}>
            Running: {games.map((g) => g.name).join(", ")}
          </div>
        </PanelSectionRow>
      )}

      {steps && (
        <PanelSectionRow>
          <StepProgress steps={steps} />
        </PanelSectionRow>
      )}

      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={handleNuke}>
          <FaPowerOff style={{ marginRight: "8px" }} />
          Nuke (Close + Sync + TV Off + Sleep)
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
          <DialogButton style={{ flex: 1, minWidth: 0 }} disabled={busy} onClick={refreshStatus}>
            Refresh
          </DialogButton>
          <DialogButton style={{ flex: 1, minWidth: 0 }} disabled={busy} onClick={onRemove}>
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

      {settings.devices.map((device) => (
        <DeviceCard
          key={device.ip}
          device={device}
          tvIp={settings.tv_ip || ""}
          onRemove={() => handleRemoveDevice(device.ip)}
        />
      ))}

      {showAddForm ? (
        <AddDeviceForm
          onAdd={() => { setShowAddForm(false); loadSettings(); }}
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

      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setShowSettings(!showSettings)}>
            {showSettings ? "Hide Settings" : "Settings"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

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
