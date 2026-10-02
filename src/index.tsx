import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaDiceD20 } from "react-icons/fa";

interface Settings {
  moai: number;
  shady: number;
  sm_total: number;
  micro: number;
  boss: number;
  challenges: number;
  magnet_max: number;
  pause_on_found: boolean;
  skip_current: boolean;
  start_delay: number;
  hotkey: string;
  near_miss_enabled: boolean;
  near_miss_stat: string;
  near_miss_minimum: number;
  near_miss_seconds: number;
}

interface MapSummary {
  moai: number;
  shady: number;
  micro: number;
  boss: number;
  magnet: number;
  challenges: number;
}

interface Requirement {
  label: string;
  value: number;
  target: string;
  met: boolean;
}

interface Closest {
  shortfall: number;
  requirements: Requirement[];
  at: number;
}

interface Odds {
  maps: number;
  hits: number;
  one_in: number;
  reliable: boolean;
  p50_seconds: number;
  p90_seconds: number;
}

interface NearMiss {
  stat: string;
  value: number;
  minimum: number;
  seconds: number;
  map: MapSummary;
}

interface Status {
  closest: Closest | null;
  odds: Odds | null;
  near_miss: NearMiss | null;
  state: string;
  message: string;
  rerolls: number;
  elapsed: number;
  last: MapSummary | null;
  found: MapSummary | null;
  target: string;
  running: boolean;
  log: string[];
}

interface UpdateInfo {
  ok: boolean;
  current: string;
  version?: string;
  url?: string;
  sha256?: string;
  page?: string;
  available: boolean;
  error?: string;
}

const PLUGIN_NAME = "BonkScanner Deck";
// Decky's own installer, the one its store uses: it shows the confirmation
// dialog, checks the SHA-256, replaces the plugin and reloads it. Settings and
// recorded maps live outside the plugin folder and survive.
const DECKY_INSTALL_TYPE_UPDATE = 2;

const checkUpdate = callable<[force: boolean], UpdateInfo>("check_update");
const getSettings = callable<[], Settings>("get_settings");
const saveSettings = callable<[settings: Settings], Settings>("save_settings");
const startScan = callable<[], Status>("start_scan");
const stopScan = callable<[], Status>("stop_scan");
const getStatus = callable<[], Status>("get_status");

const HOTKEY_OPTIONS = ["off", "L4", "R4", "L5", "R5", "L4+R4", "L5+R5"].map((value) => ({
  data: value,
  label: value === "off" ? "Off" : value,
}));

const NEAR_MISS_STATS = ["Moais", "Shady Guy", "S+M", "Microwaves", "Boss Curses", "Magnet Shrines", "Challenges"].map(
  (value) => ({ data: value, label: value }),
);

const MET = "#6FD38A";
const UNMET = "#F2A65A";

const STATE_TEXT: Record<string, string> = {
  near_miss: "Near miss - press any button to keep",
  kept: "Map kept",
  idle: "Ready",
  waiting_game: "Waiting for Megabonk...",
  starting: "Starting...",
  scanning: "Rerolling",
  waiting_unpause: "Paused - resume the game",
  found: "Target map found!",
  already_matches: "Current map already matches",
  stopped: "Stopped",
  limit: "Reroll limit reached",
  error: "Error",
};

function formatDuration(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = String(total % 60).padStart(2, "0");
  return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
}

function formatMap(map: MapSummary | null): string {
  if (!map) return "-";
  return `Moai ${map.moai} · Shady ${map.shady} · Micro ${map.micro} · Boss ${map.boss} · Magnet ${map.magnet} · Chall ${map.challenges}`;
}

function ClosestRow({ closest }: { closest: Closest }) {
  return (
    <div style={{ fontSize: "12px" }}>
      Closest (#{closest.at}):{" "}
      {closest.requirements.map((r, i) => (
        <span key={i} style={{ color: r.met ? MET : UNMET }}>
          {i > 0 ? " · " : ""}
          {r.label} <b>{r.value}</b>/{r.target}
        </span>
      ))}
    </div>
  );
}

function oddsText(odds: Odds): string {
  if (!odds.reliable) return `Odds: collecting data (${odds.maps} maps recorded)`;
  return `Odds ~1 in ${Math.round(odds.one_in)} · 50% by ${formatDuration(odds.p50_seconds)} · 90% by ${formatDuration(
    odds.p90_seconds,
  )} (${odds.hits}/${odds.maps} maps)`;
}

function UpdateSection() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    checkUpdate(false).then(setInfo).catch(() => undefined);
  }, []);

  const recheck = () => {
    setBusy(true);
    setNote("");
    checkUpdate(true)
      .then(setInfo)
      .finally(() => setBusy(false));
  };

  const install = async () => {
    if (!info?.available || !info.url || !info.sha256) return;
    const backend = (window as any).DeckyBackend;
    if (!backend?.call) {
      setNote(`Use Decky > Developer > Install Plugin from URL: ${info.url}`);
      return;
    }
    setBusy(true);
    try {
      await backend.call("utilities/install_plugin", info.url, PLUGIN_NAME, info.version, info.sha256, DECKY_INSTALL_TYPE_UPDATE);
      setNote("Confirm the update in Decky's dialog.");
    } catch (error) {
      setNote(`Update failed: ${error}`);
    } finally {
      setBusy(false);
    }
  };

  let label = "Checking for updates...";
  if (info?.available) label = `Update available: v${info.version}`;
  else if (info?.ok) label = "Up to date";
  else if (info) label = "Could not check for updates";

  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <Field label={label} bottomSeparator="none">
          {info ? `v${info.current}` : ""}
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        {info?.available ? (
          <ButtonItem layout="below" disabled={busy} onClick={install}>
            Update to v{info.version}
          </ButtonItem>
        ) : (
          <ButtonItem layout="below" disabled={busy} onClick={recheck}>
            Check again
          </ButtonItem>
        )}
      </PanelSectionRow>
      {note ? (
        <PanelSectionRow>
          <div style={{ fontSize: "11px", opacity: 0.75 }}>{note}</div>
        </PanelSectionRow>
      ) : null}
    </PanelSection>
  );
}

function Content() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    getSettings().then((s) => mounted.current && setSettings(s));
    const poll = () =>
      getStatus()
        .then((s) => mounted.current && setStatus(s))
        .catch(() => undefined);
    poll();
    const timer = setInterval(poll, 500);
    return () => {
      mounted.current = false;
      clearInterval(timer);
    };
  }, []);

  const update = (patch: Partial<Settings>) => {
    if (!settings) return;
    const next = { ...settings, ...patch };
    setSettings(next);
    saveSettings(next).then((saved) => mounted.current && setSettings(saved));
  };

  if (!settings) {
    return (
      <PanelSection>
        <PanelSectionRow>Loading...</PanelSectionRow>
      </PanelSection>
    );
  }

  const running = status?.running ?? false;
  const rate =
    status && status.rerolls > 0 && status.elapsed > 0 ? ` (${(status.elapsed / status.rerolls).toFixed(1)} s each)` : "";

  const slider = (key: keyof Settings, label: string, max: number) => (
    <PanelSectionRow>
      <SliderField
        label={label}
        value={settings[key] as number}
        min={0}
        max={max}
        step={1}
        showValue
        disabled={running}
        onChange={(value: number) => update({ [key]: value } as Partial<Settings>)}
      />
    </PanelSectionRow>
  );

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <Field label={STATE_TEXT[status?.state ?? "idle"] ?? status?.state} bottomSeparator="none">
            {status && status.rerolls > 0 ? `${status.rerolls} rerolls · ${formatDuration(status.elapsed)}${rate}` : ""}
          </Field>
        </PanelSectionRow>
        {status?.message ? (
          <PanelSectionRow>
            <div style={{ fontSize: "12px", opacity: 0.8 }}>{status.message}</div>
          </PanelSectionRow>
        ) : null}
        {status?.closest && status.closest.requirements.length && !status.found ? (
          <PanelSectionRow>
            <ClosestRow closest={status.closest} />
          </PanelSectionRow>
        ) : null}
        {status?.odds ? (
          <PanelSectionRow>
            <div style={{ fontSize: "11px", opacity: 0.75 }}>{oddsText(status.odds)}</div>
          </PanelSectionRow>
        ) : null}
        {status?.found || status?.last ? (
          <PanelSectionRow>
            <div style={{ fontSize: "12px" }}>
              {status.found ? "Found: " : "Last: "}
              {formatMap(status.found ?? status.last)}
            </div>
          </PanelSectionRow>
        ) : null}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => (running ? stopScan() : startScan()).then(setStatus)}>
            {running ? "Stop" : "Start rerolling"}
          </ButtonItem>
        </PanelSectionRow>
        {!running ? (
          <PanelSectionRow>
            <div style={{ fontSize: "11px", opacity: 0.7 }}>
              Start inside a stage 1 run, then close this menu within {settings.start_delay} s.
              {settings.hotkey !== "off" ? ` Or press ${settings.hotkey} in game to start/stop.` : ""}
            </div>
          </PanelSectionRow>
        ) : null}
      </PanelSection>

      <PanelSection title="Target (minimum)">
        {slider("moai", "Moais", 8)}
        {slider("micro", "Microwaves", 2)}
        {slider("shady", "Shady Guy", 8)}
        {slider("sm_total", "Shady + Moai", 14)}
        {slider("boss", "Boss Curses", 8)}
        {slider("challenges", "Challenges", 6)}
        <PanelSectionRow>
          <SliderField
            label="Magnet Shrines (maximum)"
            description={settings.magnet_max < 0 ? "No limit" : undefined}
            value={settings.magnet_max}
            min={-1}
            max={6}
            step={1}
            showValue
            disabled={running}
            onChange={(value: number) => update({ magnet_max: value })}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Options">
        <PanelSectionRow>
          <DropdownItem
            label="Back-button hotkey"
            description="Press in game to start / stop rerolling."
            rgOptions={HOTKEY_OPTIONS}
            selectedOption={settings.hotkey}
            onChange={(option) => update({ hotkey: option.data as string })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Pause when found"
            checked={settings.pause_on_found}
            disabled={running}
            onChange={(value: boolean) => update({ pause_on_found: value })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Reroll the current map too"
            description="Otherwise an already matching map is kept."
            checked={settings.skip_current}
            disabled={running}
            onChange={(value: boolean) => update({ skip_current: value })}
          />
        </PanelSectionRow>
        {slider("start_delay", "Start delay (seconds)", 10)}
      </PanelSection>

      <PanelSection title="Near miss">
        <PanelSectionRow>
          <ToggleField
            label="Hold near-miss maps"
            description="If one counter reaches the value, wait so you can keep the map with any button."
            checked={settings.near_miss_enabled}
            disabled={running}
            onChange={(value: boolean) => update({ near_miss_enabled: value })}
          />
        </PanelSectionRow>
        {settings.near_miss_enabled ? (
          <>
            <PanelSectionRow>
              <DropdownItem
                label="Counter"
                rgOptions={NEAR_MISS_STATS}
                selectedOption={settings.near_miss_stat}
                disabled={running}
                onChange={(option) => update({ near_miss_stat: option.data as string })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <SliderField
                label="At least"
                value={settings.near_miss_minimum}
                min={1}
                max={15}
                step={1}
                showValue
                disabled={running}
                onChange={(value: number) => update({ near_miss_minimum: value })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <SliderField
                label="Hold (seconds)"
                value={settings.near_miss_seconds}
                min={3}
                max={30}
                step={1}
                showValue
                disabled={running}
                onChange={(value: number) => update({ near_miss_seconds: value })}
              />
            </PanelSectionRow>
          </>
        ) : null}
      </PanelSection>

      <UpdateSection />

      {status?.log?.length ? (
        <PanelSection title="Log">
          <PanelSectionRow>
            <div style={{ fontSize: "10px", fontFamily: "monospace", whiteSpace: "pre-wrap", opacity: 0.7 }}>
              {status.log.join("\n")}
            </div>
          </PanelSectionRow>
        </PanelSection>
      ) : null}
    </>
  );
}

export default definePlugin(() => {
  // Registered at plugin level so the toast fires while the menu is closed.
  const listener = addEventListener<[status: Status]>("bonk_finished", (status) => {
    if (status.state === "kept") {
      toaster.toast({
        title: "BonkScanner: map kept",
        body: `${status.rerolls} rerolls in ${formatDuration(status.elapsed)} · ${formatMap(status.found)}`,
        duration: 8000,
      });
    } else if (status.state === "found" || status.state === "already_matches") {
      toaster.toast({
        title: "BonkScanner: target map found!",
        body: `${status.rerolls} rerolls in ${formatDuration(status.elapsed)} · ${formatMap(status.found)}`,
        duration: 15000,
      });
    } else if (status.state === "error") {
      toaster.toast({ title: "BonkScanner: stopped", body: status.message });
    }
  });

  const nearMissListener = addEventListener<[status: Status]>("bonk_near_miss", (status) => {
    const near = status.near_miss;
    if (!near) return;
    toaster.toast({
      title: `Near miss! ${near.stat} ${near.value} (≥${near.minimum})`,
      body: `Press any button within ${near.seconds} s to keep this map.`,
      duration: near.seconds * 1000,
    });
  });

  const hotkeyListener = addEventListener<[action: string, hotkey: string]>("bonk_hotkey", (action, hotkey) => {
    toaster.toast({
      title: action === "started" ? "BonkScanner: rerolling" : "BonkScanner: stopping",
      body: action === "started" ? `Press ${hotkey} again to stop.` : "Rerolling stopped.",
      duration: 2000,
    });
  });

  return {
    name: "BonkScanner Deck",
    titleView: <div className={staticClasses.Title}>BonkScanner Deck</div>,
    content: <Content />,
    icon: <FaDiceD20 />,
    onDismount() {
      removeEventListener("bonk_finished", listener);
      removeEventListener("bonk_hotkey", hotkeyListener);
      removeEventListener("bonk_near_miss", nearMissListener);
    },
  };
});
