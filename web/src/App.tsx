import { useCallback, useEffect, useState } from "react";
import { BookOpen, Inbox as InboxIcon, ListTodo, Repeat, Settings as SettingsIcon, Users } from "lucide-react";
import { ApiError, api, onConnection, setToken, type Health } from "./api";
import { go, useLive, useRoute } from "./hooks";
import { Automations } from "./pages/Automations";
import { Inbox } from "./pages/Inbox";
import { Knowledge } from "./pages/Knowledge";
import { Profiles } from "./pages/Profiles";
import { Settings } from "./pages/Settings";
import { TaskDetail } from "./pages/TaskDetail";
import { Tasks } from "./pages/Tasks";
import { Voice } from "./Voice";

function useHealth() {
  const [health, setHealth] = useState<Health | null>(null);
  const [down, setDown] = useState(false);
  const [locked, setLocked] = useState(false);
  const load = useCallback(
    () =>
      api.health()
        .then((h) => { setHealth(h); setDown(false); setLocked(false); })
        .catch((e) => (e instanceof ApiError && e.status === 401 ? setLocked(true) : setDown(true))),
    [],
  );
  useEffect(() => {
    load();
    const t = window.setInterval(load, 10000);
    return () => window.clearInterval(t);
  }, [load]);
  return { health, down, locked, reload: load };
}

export function App() {
  const route = useRoute();
  const section = route[0] ?? "";
  const { health, down, locked, reload: reloadHealth } = useHealth();
  const { data: inbox } = useLive(
    api.inbox,
    (ev) => ev.type.startsWith("human.") || ev.type === "subtask.status" || ev.type === "task.status" || ev.type === "plan" ||
      ev.type === "scrape.changes",
  );
  const waiting = (inbox?.plans.length ?? 0) + (inbox?.requests.length ?? 0) + (inbox?.updates.length ?? 0);
  const needsKey = health ? !health.llm_key : false;

  useEffect(() => {
    document.title = waiting ? `(${waiting}) Fleet` : "Fleet";
  }, [waiting]);

  if (locked) return <TokenGate />;

  let page;
  if (section === "tasks" && route[1]) page = <TaskDetail id={route[1]} />;
  else if (section === "inbox") page = <Inbox />;
  else if (section === "automations") page = <Automations />;
  else if (section === "profiles") page = <Profiles />;
  else if (section === "knowledge") page = <Knowledge />;
  else if (section === "settings") page = <Settings onChanged={reloadHealth} />;
  else page = <Tasks needsKey={needsKey} />;

  const nav = [
    { key: "", label: "Tasks", icon: ListTodo, active: section === "" || section === "tasks" },
    { key: "inbox", label: "Inbox", icon: InboxIcon, active: section === "inbox", badge: waiting },
    { key: "automations", label: "Automations", icon: Repeat, active: section === "automations" },
    { key: "profiles", label: "Profiles", icon: Users, active: section === "profiles" },
    { key: "knowledge", label: "Knowledge", icon: BookOpen, active: section === "knowledge" },
    { key: "settings", label: "Settings", icon: SettingsIcon, active: section === "settings", alert: needsKey },
  ];

  return (
    <div className="shell">
      <aside className="sidebar">
        <button className="brand" onClick={() => go("/")}>
          <span className="logo">
            <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
              <path d="M9 22l7-12 7 12" stroke="currentColor" strokeWidth="3" fill="none" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          Fleet
        </button>
        <nav>
          {nav.map((n) => (
            <button key={n.key} className={`nav ${n.active ? "active" : ""}`} onClick={() => go(`/${n.key}`)}>
              <n.icon size={17} />
              <span>{n.label}</span>
              {!!n.badge && <span className="badge">{n.badge}</span>}
              {n.alert && <span className="alert-dot" title="Needs setup" />}
            </button>
          ))}
        </nav>
        <span className="spacer" />
        <Voice plans={inbox?.plans ?? []} />
        <SystemStatus health={health} down={down} />
      </aside>
      <main className="main">{page}</main>
    </div>
  );
}

/** Shown when the server has FLEET_API_TOKEN set and this browser doesn't have it yet. */
function TokenGate() {
  const [value, setValue] = useState("");
  return (
    <div className="page gate">
      <h1>Fleet is locked</h1>
      <p className="muted">This Fleet server needs its access token (FLEET_API_TOKEN). It's stored in this browser only.</p>
      <form
        className="row gap"
        onSubmit={(e) => {
          e.preventDefault();
          setToken(value.trim());
          window.location.reload();
        }}
      >
        <input className="input grow" type="password" autoComplete="off" placeholder="Access token" value={value}
          onChange={(e) => setValue(e.target.value)} />
        <button className="btn primary" disabled={!value.trim()}>Unlock</button>
      </form>
    </div>
  );
}

function SystemStatus({ health, down }: { health: Health | null; down: boolean }) {
  const [stream, setStream] = useState(false);
  useEffect(() => onConnection(setStream), []);

  const rows: [string, boolean | undefined, string][] = [
    ["Server", !down, down ? "not reachable" : "running"],
    ["Live updates", stream, stream ? "connected" : "reconnecting"],
    ["AdsPower", health?.adspower, health?.adspower ? "connected" : "not running"],
    ["OpenRouter key", health?.llm_key, health?.vault_error ? "vault error" : health?.llm_key ? "saved" : "add it in Settings"],
  ];
  return (
    <div className="status">
      {rows.map(([label, ok, text]) => (
        <div key={label} className="status-row" title={label === "OpenRouter key" && health?.vault_error ? health.vault_error : text}>
          <span className={`dot ${ok ? "ok" : ok === undefined ? "" : "bad"}`} />
          <span>{label}</span>
          <span className="muted small status-text">{text}</span>
        </div>
      ))}
      {health && <div className="muted tiny">Planner {health.models.planner}</div>}
    </div>
  );
}
