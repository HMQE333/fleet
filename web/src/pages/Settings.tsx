import { useEffect, useMemo, useState } from "react";
import { Check, Eye, EyeOff, KeyRound, Lock, RotateCcw, ShieldCheck, Trash2, UserPlus, Zap } from "lucide-react";
import { api, type CatalogModel, type CostSummary, type Identity, type KeyCheck, type LimitRow, type NotifyConfig, type StorageUsage, type ModelRole, type Profile, type SecretInfo, type SiteAccount, type TrustRule } from "../api";
import { ago, money } from "../format";

export function Settings({ onChanged }: { onChanged: () => void }) {
  return (
    <div className="page">
      <h1>Settings</h1>
      <ApiKeys onChanged={onChanged} />
      <SiteLogins />
      <YourDetails />
      <TrustRules />
      <Limits />
      <Costs />
      <Storage />
      <Notifications />
      <FastBrowser />
      <Models />
    </div>
  );
}

// ---- API keys ---------------------------------------------------------------------

function ApiKeys({ onChanged }: { onChanged: () => void }) {
  const [secrets, setSecrets] = useState<SecretInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () =>
    api.secrets().then((s) => { setSecrets(s); setError(null); }).catch((e) => setError(String(e.message ?? e)));
  useEffect(() => { load(); }, []);

  return (
    <section className="stack">
      <div className="section-head"><h2>API keys</h2></div>
      <p className="muted">
        <Lock size={13} className="inline-icon" /> Keys are encrypted before they're stored. The encryption key lives in
        Windows Credential Manager, not in the database, and a saved key can never be shown again — only replaced or
        deleted. Fleet also scrubs them from logs.
      </p>
      {error && <div className="notice bad-notice">{error}</div>}
      {secrets?.map((s) => <SecretRow key={s.name} s={s} onSaved={() => { load(); onChanged(); }} />)}
    </section>
  );
}

function SecretRow({ s, onSaved }: { s: SecretInfo; onSaved: () => void }) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [check, setCheck] = useState<KeyCheck | null>(null);

  async function save() {
    setBusy(true);
    setErr(null);
    setCheck(null);
    try {
      await api.putSecret(s.name, value.trim());
      setValue("");
      onSaved();
      if (s.name === "OPENROUTER_API_KEY") setCheck(await api.checkOpenRouter());
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!window.confirm(`Delete ${s.label} key? Anything that needs it will stop working.`)) return;
    try {
      await api.deleteSecret(s.name);
      setCheck(null);
      onSaved();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="card secret">
      <div className="row gap center wrap">
        <KeyRound size={16} />
        <strong>{s.label}</strong>
        <code className="muted small">{s.name}</code>
        <span className="spacer" />
        {s.set ? (
          <span className="pill tone-ok"><Check size={12} /> Saved {s.hint}</span>
        ) : (
          <span className={`pill ${s.required ? "tone-warn" : "tone-neutral"}`}>{s.required ? "Required" : "Optional"}</span>
        )}
      </div>
      {s.help && <p className="muted small">{s.help}</p>}
      {s.set && (
        <p className="muted small">
          Updated {ago(s.updated_at)}{s.last_used_at ? ` · last used ${ago(s.last_used_at)}` : " · not used yet"}
        </p>
      )}
      <div className="row gap wrap">
        <input
          className="input grow"
          type="password"
          autoComplete="off"
          spellCheck={false}
          placeholder={s.set ? "Paste a new key to replace the saved one" : "Paste the key"}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && value.trim() && save()}
        />
        <button className="btn primary" disabled={busy || !value.trim()} onClick={save}>
          {s.set ? "Replace" : "Save"}
        </button>
        {s.set && s.name === "OPENROUTER_API_KEY" && (
          <button className="btn" onClick={async () => setCheck(await api.checkOpenRouter())}>
            <ShieldCheck size={15} /> Check
          </button>
        )}
        {s.set && (
          <button className="btn danger-ghost" onClick={remove} title="Delete key">
            <Trash2 size={15} />
          </button>
        )}
      </div>
      {check && (
        check.ok ? (
          <div className="notice">
            OpenRouter accepted the key{check.label ? ` “${check.label}”` : ""}.
            {check.limit_remaining != null ? ` $${Number(check.limit_remaining).toFixed(2)} left on its limit.` : " No spending limit set on this key."}
            {check.usage != null ? ` $${Number(check.usage).toFixed(2)} used so far.` : ""}
          </div>
        ) : (
          <div className="notice bad-notice">{check.error}</div>
        )
      )}
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

// ---- site logins --------------------------------------------------------------------

function SiteLogins() {
  const [rows, setRows] = useState<SiteAccount[] | null>(null);
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const empty = { domain: "", username: "", password: "", profile: "", auto: false };
  const [form, setForm] = useState(empty);

  const load = () =>
    api.siteAccounts().then((r) => { setRows(r); setError(null); }).catch((e) => setError(String(e.message ?? e)));
  useEffect(() => {
    load();
    api.profiles().then(setProfiles).catch(() => setProfiles([]));
  }, []);

  async function add() {
    setBusy(true);
    setError(null);
    try {
      await api.createSiteAccount({
        domain: form.domain.trim(),
        username: form.username.trim(),
        password: form.password,
        profile_id: form.profile || null,
        auto_login: form.auto,
      });
      setForm(empty);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const ready = form.domain.trim() && form.username.trim() && form.password;
  return (
    <section className="stack">
      <div className="section-head"><h2>Site logins</h2></div>
      <p className="muted">
        <Lock size={13} className="inline-icon" /> When a task reaches a site's login form, Fleet's code types the login
        you saved here — the AI model never sees it, and it's never shown again or written to logs. By default the
        Inbox asks first. Only sign-in forms are filled, never sign-up forms, and Fleet tries once per run so a wrong
        password can't lock you out. You can always log in by hand instead.
      </p>
      {error && <div className="notice bad-notice">{error}</div>}
      {rows?.map((a) => <SiteLoginRow key={a.id} a={a} onChanged={load} />)}
      {rows && rows.length === 0 && <div className="muted small">No saved logins yet.</div>}
      <div className="card secret">
        <strong>Add a login</strong>
        <div className="login-form">
          <input className="input" placeholder="Site, like example.com" value={form.domain}
            onChange={(e) => setForm({ ...form, domain: e.target.value })} />
          <input className="input" placeholder="Username or email" autoComplete="off" value={form.username}
            onChange={(e) => setForm({ ...form, username: e.target.value })} />
          <input className="input" type="password" placeholder="Password" autoComplete="new-password" value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })} />
          <select className="input" value={form.profile} onChange={(e) => setForm({ ...form, profile: e.target.value })}>
            <option value="">Any profile</option>
            {profiles.map((p) => <option key={p.id} value={p.id}>Only profile “{p.name}”</option>)}
          </select>
        </div>
        <label className="toggle small">
          <input type="checkbox" checked={form.auto} onChange={(e) => setForm({ ...form, auto: e.target.checked })} />
          <span>
            Log in automatically, without asking
            <span className="muted block">Leave this off to approve each use in the Inbox.</span>
          </span>
        </label>
        <div className="row gap">
          <button className="btn primary" disabled={busy || !ready} onClick={add}>Save login</button>
        </div>
      </div>
    </section>
  );
}

function SiteLoginRow({ a, onChanged }: { a: SiteAccount; onChanged: () => void }) {
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function call(fn: () => Promise<unknown>) {
    setErr(null);
    try {
      await fn();
      setPassword("");
      onChanged();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="card secret">
      <div className="row gap center wrap">
        <KeyRound size={16} />
        <strong>{a.domain}</strong>
        <span className="muted small">{a.username}</span>
        <span className="pill tone-neutral">{a.profile_name ? `profile “${a.profile_name}”` : "any profile"}</span>
        {a.origin === "fleet" && <span className="pill tone-ok">{a.label || "Registered by Fleet"}</span>}
        <span className="spacer" />
        <button className="btn danger-ghost" title="Delete this saved login"
          onClick={() => window.confirm(`Delete the saved login for ${a.domain}?`) && call(() => api.deleteSiteAccount(a.id))}>
          <Trash2 size={15} />
        </button>
      </div>
      <p className="muted small">
        {a.last_used_at ? `Last used ${ago(a.last_used_at)}` : "Not used yet"}
        {a.last_failed_at ? ` · didn't work ${ago(a.last_failed_at)} — replace the password if it changed` : ""}
      </p>
      <label className="toggle small">
        <input type="checkbox" checked={a.auto_login}
          onChange={(e) => call(() => api.patchSiteAccount(a.id, { auto_login: e.target.checked }))} />
        <span>
          Log in automatically, without asking
          <span className="muted block">Off: the Inbox asks each time and you can say “I'll do it myself”.</span>
        </span>
      </label>
      <div className="row gap wrap">
        <input className="input grow" type="password" autoComplete="new-password" value={password}
          placeholder="Paste a new password to replace the saved one" onChange={(e) => setPassword(e.target.value)} />
        <button className="btn" disabled={!password} onClick={() => call(() => api.patchSiteAccount(a.id, { password }))}>
          Replace
        </button>
      </div>
      {a.origin === "fleet" && <RevealPassword id={a.id} />}
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

/** Fleet generated this account's password, so the operator can read it (hidden again after 30 s). */
function RevealPassword({ id }: { id: string }) {
  const [shown, setShown] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (shown === null) return;
    const timer = window.setTimeout(() => setShown(null), 30_000);
    return () => window.clearTimeout(timer);
  }, [shown]);
  return (
    <div className="row gap center wrap">
      {shown === null ? (
        <button className="btn" onClick={() => api.revealSiteAccount(id).then((r) => { setShown(r.password); setErr(null); })
          .catch((e) => setErr(e instanceof Error ? e.message : String(e)))}>
          <Eye size={15} /> Show password
        </button>
      ) : (
        <>
          <code className="selectable">{shown}</code>
          <button className="btn" onClick={() => setShown(null)}><EyeOff size={15} /> Hide</button>
        </>
      )}
      <span className="muted small">Fleet generated this password. Showing it is logged.</span>
      {err && <span className="error small">{err}</span>}
    </div>
  );
}

// ---- your details (for sign-ups) -------------------------------------------------------------------------------

function YourDetails() {
  const [form, setForm] = useState<Identity | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    api.identity().then(setForm).catch((e) => setMsg({ ok: false, text: String(e.message ?? e) }));
  }, []);
  const set = (k: keyof Identity) => (e: React.ChangeEvent<HTMLInputElement>) => form && setForm({ ...form, [k]: e.target.value });

  async function save() {
    if (!form) return;
    try {
      setForm(await api.saveIdentity(form));
      setMsg({ ok: true, text: "Saved." });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <section className="stack">
      <div className="section-head"><h2>Your details for sign-ups</h2></div>
      <p className="muted">
        <UserPlus size={13} className="inline-icon" /> Only used when you allow a task to create an account: Fleet types
        these into the site's sign-up form after you approve it in the Inbox, plus a strong password it generates (saved
        encrypted; you can show it next to the login above). Use your own, real details. Fleet never invents people,
        creates at most a few accounts a day, and leaves email confirmations and captchas to you.
      </p>
      {form && (
        <div className="card secret">
          <div className="login-form">
            <input className="input" placeholder="First name" value={form.first_name} onChange={set("first_name")} />
            <input className="input" placeholder="Last name" value={form.last_name} onChange={set("last_name")} />
            <input className="input" type="email" placeholder="Email address (required)" value={form.email} onChange={set("email")} />
            <input className="input" placeholder="Username (optional — defaults to the part before the @)" value={form.username}
              onChange={set("username")} />
          </div>
          <div className="row gap center">
            <button className="btn primary" onClick={save}>Save details</button>
            {msg && <span className={msg.ok ? "muted small" : "error small"}>{msg.text}</span>}
          </div>
          <NeverSignUp />
        </div>
      )}
    </section>
  );
}

// ---- never sign up here ------------------------------------------------------------------------------------------

function NeverSignUp() {
  const [sites, setSites] = useState<string[] | null>(null);
  const [value, setValue] = useState("");
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { api.noRegister().then(setSites).catch((e) => setErr(String(e.message ?? e))); }, []);
  const run = (p: Promise<string[]>) => p.then((s) => { setSites(s); setErr(null); setValue(""); }).catch((e) => setErr(e instanceof Error ? e.message : String(e)));
  return (
    <div className="stack small">
      <strong>Never sign up on these sites</strong>
      <span className="muted">Fleet refuses to create an account on a listed site (or its subdomains), whatever a task allows.</span>
      {sites?.map((d) => (
        <div className="row gap center" key={d}>
          <span>{d}</span>
          <button className="link small" onClick={() => run(api.removeNoRegister(d))}>remove</button>
        </div>
      ))}
      <div className="row gap center wrap">
        <input className="input" placeholder="example.com" value={value} onChange={(e) => setValue(e.target.value)} style={{ width: 220 }} />
        <button className="btn" disabled={value.trim().length < 3} onClick={() => run(api.addNoRegister(value.trim()))}>Add</button>
      </div>
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

// ---- trust rules ---------------------------------------------------------------------------------------------

function TrustRules() {
  const [rules, setRules] = useState<TrustRule[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const load = () => api.trustRules().then((r) => { setRules(r); setErr(null); }).catch((e) => setErr(String(e.message ?? e)));
  useEffect(() => { load(); }, []);

  return (
    <section className="stack">
      <div className="section-head"><h2>Trust rules</h2></div>
      <p className="muted">
        <ShieldCheck size={13} className="inline-icon" /> When the Inbox asks you to approve a click, you can answer
        “approve and always allow”. Fleet's fast browser then clicks that exact button on that exact site, in that
        profile, without asking, and logs each use. Payments, deleting, accepting terms, signing up and vague buttons
        like “OK” or “Confirm” always ask. Remove a rule here and Fleet asks again.
      </p>
      {err && <div className="notice bad-notice">{err}</div>}
      {rules?.map((r) => (
        <div className="card secret" key={r.id}>
          <div className="row gap center wrap">
            <ShieldCheck size={16} />
            <strong>“{r.label}”</strong>
            <span className="muted small">on {r.domain}</span>
            <span className="pill tone-neutral">profile “{r.profile_name}”</span>
            <span className="spacer" />
            <button className="btn danger-ghost" title="Remove this rule"
              onClick={() => api.deleteTrustRule(r.id).then(load).catch((e) => setErr(String(e.message ?? e)))}>
              <Trash2 size={15} />
            </button>
          </div>
          <p className="muted small">
            {r.uses ? `Used ${r.uses} time${r.uses === 1 ? "" : "s"}, last ${ago(r.last_used_at ?? r.created_at)}` : "Not used yet"}
          </p>
        </div>
      ))}
      {rules && rules.length === 0 && <div className="muted small">No rules yet. Every irreversible click asks you.</div>}
    </section>
  );
}

// ---- limits ---------------------------------------------------------------------------------------------------

function Limits() {
  const [rows, setRows] = useState<LimitRow[] | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    api.limits().then(setRows).catch((e) => setMsg({ ok: false, text: String(e.message ?? e) }));
  }, []);

  const changes = Object.fromEntries(
    Object.entries(draft)
      .filter(([name, v]) => v.trim() !== "" && Number(v) !== rows?.find((r) => r.name === name)?.value)
      .map(([name, v]) => [name, Number(v)]),
  );

  async function save(values: Record<string, number | null>, done: string) {
    try {
      setRows(await api.saveLimits(values));
      setDraft({});
      setMsg({ ok: true, text: done });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <section className="stack">
      <div className="section-head"><h2>Limits</h2></div>
      <p className="muted">
        How much one step may do: pages, depth, items, time, accounts per day. Changes apply to the next step, no restart.
        Each has a hard ceiling. The pause between requests to one site, robots.txt and the public-address checks aren't
        editable here on purpose.
      </p>
      {rows && (
        <div className="card secret">
          {rows.map((r) => (
            <div className="row gap center wrap" key={r.name}>
              <label className="grow" htmlFor={`limit-${r.name}`}>
                {r.label}
                <span className="muted small block">
                  Default {r.default}, allowed {r.min}–{r.max}{r.overridden ? " · changed by you" : ""}
                </span>
              </label>
              <input id={`limit-${r.name}`} className="input" type="number" min={r.min} max={r.max} step={1} style={{ width: 110 }}
                value={draft[r.name] ?? String(r.value)} onChange={(e) => setDraft({ ...draft, [r.name]: e.target.value })} />
              {r.overridden && (
                <button className="btn" title="Back to the default" onClick={() => save({ [r.name]: null }, "Back to the default.")}>
                  <RotateCcw size={14} />
                </button>
              )}
            </div>
          ))}
          <div className="row gap center">
            <button className="btn primary" disabled={Object.keys(changes).length === 0} onClick={() => save(changes, "Saved. The next step uses these.")}>
              Save limits
            </button>
            {msg && <span className={msg.ok ? "muted small" : "error small"}>{msg.text}</span>}
          </div>
        </div>
      )}
      {!rows && msg && <div className="error small">{msg.text}</div>}
    </section>
  );
}

// ---- storage --------------------------------------------------------------------------------------------------

const mb = (bytes: number) => (bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`);

function Storage() {
  const [use, setUse] = useState<StorageUsage | null>(null);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const load = () => api.storageUsage().then(setUse).catch((e) => setMsg({ ok: false, text: String(e.message ?? e) }));
  useEffect(() => { load(); }, []);

  async function clean() {
    try {
      const r = await api.pruneNow();
      setMsg({ ok: true, text: `Removed ${r.files} files (${mb(r.bytes)}) and ${r.events} step log lines of ${r.tasks} finished tasks.` });
      load();
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <section className="stack">
      <div className="section-head"><h2>Storage</h2></div>
      <p className="muted">
        Screenshots of browser steps, saved agent histories and the step-by-step log grow with every task. Set “Keep
        screenshots and step-by-step logs for N days” under Limits and Fleet cleans up finished tasks older than that,
        about every six hours. Answers, scraped data, saved automations and the record of your approvals are never removed.
      </p>
      {use && (
        <div className="card secret">
          <div className="row gap wrap small">
            <span>Screenshots: <strong>{mb(use.screenshots_bytes)}</strong></span>
            <span>Histories: <strong>{mb(use.histories_bytes)}</strong></span>
            <span>Step log lines: <strong>{use.step_events}</strong></span>
            <span className="muted">{use.retention_days > 0 ? `Keeping ${use.retention_days} days` : "Keeping everything"}</span>
          </div>
          <div className="row gap center">
            <button className="btn" disabled={use.retention_days <= 0} onClick={clean}
              title={use.retention_days <= 0 ? "Set a number of days under Limits first" : "Clean up now"}>
              Clean up now
            </button>
            {msg && <span className={msg.ok ? "muted small" : "error small"}>{msg.text}</span>}
          </div>
        </div>
      )}
    </section>
  );
}

// ---- costs ------------------------------------------------------------------------------------------------------

const COST_PERIODS = [7, 30, 90] as const;

function Costs() {
  const [days, setDays] = useState<number>(30);
  const [sum, setSum] = useState<CostSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api.costSummary(days)
      .then((s) => { if (live) { setSum(s); setError(null); } })
      .catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [days]);

  return (
    <section className="stack">
      <div className="section-head">
        <h2>Costs</h2>
        <span className="spacer" />
        <div className="row gap small">
          {COST_PERIODS.map((d) => (
            <button key={d} className={`btn ${d === days ? "primary" : ""}`} onClick={() => setDays(d)}>{d} days</button>
          ))}
        </div>
      </div>
      <p className="muted">
        What Fleet spent on model calls, by role: each step kind, plus “planner” for the part of a task's bill that no step
        accounts for (planning, review, summaries). Below it, every saved automation that ran in the period: what its first run
        cost against what its replays cost, and the difference. A negative saving means the replays got dearer than the first
        run (a path that drifted and fell back to the model every time): worth re-recording.
      </p>
      {error && <p className="error small">{error}</p>}
      {sum && (
        <div className="card secret">
          <div className="row gap wrap small">
            <span>Last {sum.days} days: <strong>{money(sum.total_usd)}</strong></span>
            <span className="muted">{sum.tasks} tasks</span>
          </div>
          <div className="data-table">
            <table>
              <thead><tr><th>Role</th><th>Steps</th><th>Spent</th></tr></thead>
              <tbody>
                {sum.by_role.map((r) => (
                  <tr key={r.role}><td>{r.role}</td><td>{r.steps}</td><td>{money(r.usd)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
          {sum.automations.length > 0 && (
            <div className="data-table"><table>
              <thead><tr><th>Automation</th><th>First run</th><th>Replays</th><th>Replays cost</th><th>Saved</th></tr></thead>
              <tbody>
                {sum.automations.map((a) => (
                  <tr key={a.automation_id}>
                    <td>{a.name}</td>
                    <td>{money(a.first_run_usd)}</td>
                    <td>{a.replays}</td>
                    <td>{money(a.replay_total_usd)}</td>
                    <td className={a.saved_usd < 0 ? "error" : ""}>{a.saved_usd < 0 ? `−${money(-a.saved_usd)}` : money(a.saved_usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </div>
      )}
    </section>
  );
}

// ---- notifications --------------------------------------------------------------------------------------------

function Notifications() {
  const [cfg, setCfg] = useState<NotifyConfig | null>(null);
  const [url, setUrl] = useState("");
  const [on, setOn] = useState<string[]>([]);
  const [format, setFormat] = useState<"json" | "plain">("json");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const apply = (c: NotifyConfig) => { setCfg(c); setOn(c.on); setFormat(c.format); setUrl(""); };
  useEffect(() => { api.notifications().then(apply).catch((e) => setMsg({ ok: false, text: String(e.message ?? e) })); }, []);

  async function call(fn: () => Promise<NotifyConfig | { ok: true }>, done: string) {
    try {
      const r = await fn();
      if ("triggers" in r) apply(r);
      setMsg({ ok: true, text: done });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  }

  return (
    <section className="stack">
      <div className="section-head"><h2>Notifications</h2></div>
      <p className="muted">
        Optional. Fleet can send one short line to a webhook you enter (Discord, Slack, ntfy, your own) when something needs you, when an
        automation finds changes, or when a task finishes. Nothing leaves this computer until you set this up and press “Send a test”.
        Only that line is sent, never a password or key. The address stays hidden here, and it must be a public https address.
      </p>
      {cfg && (
        <div className="card secret">
          <div className="small">{cfg.configured ? <>Sending to <strong>{cfg.host}</strong></> : "Not set up"}</div>
          <input className="input" type="password" autoComplete="off" value={url} onChange={(e) => setUrl(e.target.value)}
            placeholder={cfg.configured ? "Paste a new address to replace the saved one" : "https://… (webhook address)"} />
          {cfg.triggers.map((t) => (
            <label className="toggle small" key={t.name}>
              <input type="checkbox" checked={on.includes(t.name)}
                onChange={(e) => setOn(e.target.checked ? [...on, t.name] : on.filter((n) => n !== t.name))} />
              <span>{t.label}</span>
            </label>
          ))}
          <label className="small">
            Format{" "}
            <select className="input" value={format} onChange={(e) => setFormat(e.target.value as "json" | "plain")} style={{ width: 150 }}>
              <option value="json">JSON (Discord, Slack…)</option>
              <option value="plain">Plain text (ntfy…)</option>
            </select>
          </label>
          <div className="row gap center wrap">
            <button className="btn primary" disabled={!cfg.configured && !url.trim()}
              onClick={() => call(() => api.saveNotifications({ url: url.trim() || null, on, format }), "Saved.")}>Save</button>
            <button className="btn" disabled={!cfg.configured} onClick={() => call(() => api.testNotification(), "Sent. Check your channel.")}>
              Send a test
            </button>
            {cfg.configured && (
              <button className="btn danger-ghost" onClick={() => call(() => api.saveNotifications({ url: "", on: [], format }), "Removed.")}>
                Remove
              </button>
            )}
            {msg && <span className={msg.ok ? "muted small" : "error small"}>{msg.text}</span>}
          </div>
        </div>
      )}
    </section>
  );
}

// ---- fast browser -----------------------------------------------------------------

function FastBrowser() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.fastBrowser().then((r) => setEnabled(r.enabled)).catch((e) => setErr(String(e.message ?? e)));
  }, []);
  return (
    <section className="stack">
      <div className="section-head"><h2>Fast browser</h2></div>
      <div className="card secret">
        <label className="toggle">
          <input type="checkbox" checked={!!enabled} disabled={enabled === null}
            onChange={async (e) => {
              try {
                setEnabled((await api.setFastBrowser(e.target.checked)).enabled);
              } catch (er) {
                setErr(er instanceof Error ? er.message : String(er));
              }
            }} />
          <span>
            <strong><Zap size={14} className="inline-icon" /> Use the fast browser for simple browser steps</strong>
            <span className="muted small block">
              A small decision model (Jev) picks one click at a time from what's on the page — seconds and fractions of
              a cent per step instead of a vision model reading screenshots. Saved automations replay the path a
              browser step took, asking Jev only where the page changed. If it gets stuck, the full browser agent takes
              over automatically. Same approvals, captcha and login hand-offs as the full agent.
            </span>
          </span>
        </label>
        {err && <div className="error small">{err}</div>}
      </div>
    </section>
  );
}

// ---- models -----------------------------------------------------------------------

/** Which API a model id runs on — the same split as fleet/providers.py. */
const providerOf = (id: string) => (id.startsWith("anthropic-api/") ? "anthropic-api" : "openrouter");

function Models() {
  const [roles, setRoles] = useState<ModelRole[] | null>(null);
  const [catalog, setCatalog] = useState<CatalogModel[]>([]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    api.modelRoles().then(setRoles).catch((e) => setMsg({ ok: false, text: String(e.message ?? e) }));
    api.catalog().then(setCatalog).catch(() => setCatalog([]));
  }, []);

  const byId = useMemo(() => new Map(catalog.map((m) => [m.id, m])), [catalog]);
  const changes = Object.fromEntries(
    Object.entries(draft).filter(([role, id]) => roles?.find((r) => r.role === role)?.model !== id.trim()),
  );

  async function save() {
    setBusy(true);
    setMsg(null);
    try {
      setRoles(await api.saveModels(Object.fromEntries(Object.entries(changes).map(([k, v]) => [k, v.trim()]))));
      setDraft({});
      setMsg({ ok: true, text: "Saved. New subtasks use these models." });
    } catch (e) {
      setMsg({ ok: false, text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="stack">
      <div className="section-head">
        <h2>Models</h2>
        <span className="muted small">{catalog.length ? `${catalog.length} models available` : "model list unavailable"}</span>
      </div>
      <p className="muted">
        Every job runs on the model you pick here — mix providers freely (e.g. Claude to plan, Perplexity to research,
        Gemini for video). Prices are per million tokens. Everything goes through OpenRouter, except an{" "}
        <code>anthropic-api/…</code> model: the planner and the readers can run on Anthropic's own API with the Claude
        key you saved above.
      </p>
      {roles?.map((r) => {
        const value = draft[r.role] ?? r.model;
        const info = byId.get(value.trim());
        const options = catalog.filter(
          (m) => r.needs.every((n) => m.capabilities.includes(n)) && r.providers.includes(providerOf(m.id)),
        );
        const missing = info ? r.needs.filter((n) => !info.capabilities.includes(n)) : [];
        return (
          <div key={r.role} className="card model-role">
            <div className="row gap center wrap">
              <strong>{r.label}</strong>
              {r.needs.length > 0 && <span className="muted small">needs {r.needs.join(" + ")}</span>}
              <span className="spacer" />
              {value !== r.default && (
                <button className="link small" onClick={() => setDraft({ ...draft, [r.role]: r.default })}>
                  <RotateCcw size={11} /> default
                </button>
              )}
            </div>
            <p className="muted small">{r.help}</p>
            <input
              className="input mono"
              list={`models-${r.role}`}
              value={value}
              spellCheck={false}
              onChange={(e) => setDraft({ ...draft, [r.role]: e.target.value })}
            />
            <datalist id={`models-${r.role}`}>
              {options.map((m) => <option key={m.id} value={m.id}>{`${m.name} — $${m.input_per_m} / $${m.output_per_m}`}</option>)}
            </datalist>
            <div className="small">
              {r.offcatalog ? (
                <span className="muted">Runs through OpenRouter's decisions API (not in the chat model list).</span>
              ) : info ? (
                <span className="muted">
                  {info.name} · ${info.input_per_m} in / ${info.output_per_m} out
                  {info.context ? ` · ${Math.round(info.context / 1000)}k context` : ""}
                </span>
              ) : catalog.length ? (
                <span className="error">
                  {providerOf(value.trim()) === "anthropic-api" ? "Not a Claude model Fleet knows" : "Not found on OpenRouter"}
                </span>
              ) : null}
              {!r.offcatalog && missing.length > 0 && <span className="error"> · lacks {missing.join(", ")}</span>}
            </div>
          </div>
        );
      })}
      <div className="row gap center">
        <button className="btn primary" disabled={busy || Object.keys(changes).length === 0} onClick={save}>
          Save models
        </button>
        {msg && <span className={msg.ok ? "muted small" : "error small"}>{msg.text}</span>}
      </div>
    </section>
  );
}
