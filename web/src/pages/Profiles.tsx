import { useState } from "react";
import { ExternalLink, Plus, RefreshCw, RotateCcw, Users } from "lucide-react";
import { api, type Profile } from "../api";
import { Empty } from "../components";
import { ago } from "../format";
import { useLive } from "../hooks";

export function Profiles() {
  const { data, error, reload } = useLive(api.profiles, (ev) => ev.type === "subtask.status", []);
  const [syncing, setSyncing] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [syncErr, setSyncErr] = useState<string | null>(null);

  async function addLocal() {
    const name = window.prompt("Name for the local profile (a Chrome / Edge profile Fleet runs itself, no AdsPower):", "Scratch");
    if (!name || !name.trim()) return;
    setMsg(null);
    setSyncErr(null);
    try {
      const r = await api.addLocalProfile({ name: name.trim() });
      setMsg(`Added local profile “${r.name}”, switched on for general browsing. It runs headless; press Open when you want to see its window.`);
      reload();
    } catch (e) {
      setSyncErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function sync() {
    setSyncing(true);
    setMsg(null);
    setSyncErr(null);
    try {
      const r = await api.syncProfiles();
      setMsg(`Found ${r.seen} profiles in AdsPower${r.added ? `, ${r.added} new` : ""}.`);
      reload();
    } catch (e) {
      setSyncErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSyncing(false);
    }
  }

  return (
    <div className="page wide">
      <div className="section-head">
        <h1>Profiles</h1>
        <span className="spacer" />
        <button className="btn" onClick={addLocal} title="A Chrome / Edge profile Fleet runs itself, no AdsPower needed">
          <Plus size={15} /> Add a local profile
        </button>
        <button className="btn" onClick={sync} disabled={syncing}>
          <RefreshCw size={15} className={syncing ? "spin" : ""} /> Sync from AdsPower
        </button>
      </div>
      <p className="muted">
        Each profile is one of your real accounts: an AdsPower profile with its own fingerprint and fixed proxy, or a local
        Chrome / Edge profile Fleet runs itself. Fleet only uses profiles you switch on, one worker per profile at a time.
        Log in to each account once by hand in its window (Open) — Fleet reuses the session and never handles passwords.
        The notes tell the planner what each account is for.
      </p>
      {msg && <div className="notice">{msg}</div>}
      {syncErr && <div className="notice bad-notice">{syncErr}</div>}
      {error && <div className="error">{error}</div>}
      {data && data.length === 0 && (
        <Empty icon={Users} title="No profiles yet">
          Add a local profile (no AdsPower needed), or open AdsPower (Local API on in Settings → API) and click Sync.
        </Empty>
      )}
      {data && data.length > 0 && (
        <div className="profiles">
          {data.map((p) => <ProfileRow key={p.id} p={p} onChange={reload} />)}
        </div>
      )}
    </div>
  );
}

function ProfileRow({ p, onChange }: { p: Profile; onChange: () => void }) {
  const [notes, setNotes] = useState(p.notes);
  const [domains, setDomains] = useState(p.allowed_domains.join(", "));
  const [err, setErr] = useState<string | null>(null);
  const dirty = notes !== p.notes || domains !== p.allowed_domains.join(", ");

  async function patch(body: Parameters<typeof api.patchProfile>[1]) {
    setErr(null);
    try {
      await api.patchProfile(p.id, body);
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className={`profile ${p.enabled ? "" : "off"}`}>
      <label className="switch" title={p.enabled ? "Fleet may use this profile" : "Fleet won't touch this profile"}>
        <input type="checkbox" checked={p.enabled} onChange={(e) => patch({ enabled: e.target.checked })} />
        <span />
      </label>
      <div className="profile-main">
        <div className="row gap center wrap">
          <strong>{p.name}</strong>
          <span className="muted small">{p.adspower_id}{p.group_name ? ` · ${p.group_name}` : ""}</span>
          <span className={`pill tone-${p.status === "busy" ? "info" : p.status === "error" ? "bad" : "neutral"}`}>
            {p.status === "busy" ? "In use" : p.status === "error" ? "Error" : "Idle"}
          </span>
          {p.general_use && <span className="pill tone-accent">general browsing</span>}
          {p.last_used_at && <span className="muted small">used {ago(p.last_used_at)}</span>}
        </div>
        <div className="profile-fields">
          <input className="input" placeholder="What is this account? e.g. “RybiaPaka Buffer + Instagram, logged in”" value={notes} onChange={(e) => setNotes(e.target.value)} />
          <input className="input" placeholder="Allowed domains (optional): buffer.com, instagram.com" value={domains} onChange={(e) => setDomains(e.target.value)} />
          {dirty && (
            <button className="btn primary" onClick={() => patch({ notes, allowed_domains: domains.split(/[\s,]+/).filter(Boolean) })}>
              Save
            </button>
          )}
        </div>
        <label className="toggle small">
          <input type="checkbox" checked={p.general_use} onChange={(e) => patch({ general_use: e.target.checked })} />
          <span>
            Use for general browsing
            <span className="muted block">
              Steps that don't name an account run only on profiles with this on. Leave it off for profiles logged in to
              accounts you care about — they're then used only when a plan names them.
            </span>
          </span>
        </label>
        {p.status === "error" && p.last_error && (
          <div className="error small">
            {p.last_error}{" "}
            <button className="link small" onClick={() => patch({ reset: true })}>
              <RotateCcw size={11} /> Reset
            </button>
          </div>
        )}
        {err && <div className="error small">{err}</div>}
      </div>
      <button className="btn ghost" onClick={() => api.openProfile(p.id).catch((e) => setErr(String(e.message ?? e)))} title={p.kind === "local" ? "Open this profile's window (it runs headless otherwise)" : "Open this profile's window in AdsPower"}>
        <ExternalLink size={15} /> Open
      </button>
    </div>
  );
}
