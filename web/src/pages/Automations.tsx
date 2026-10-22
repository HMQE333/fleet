import { useEffect, useState } from "react";
import { Bell, ChevronDown, ChevronRight, Play, Repeat, Trash2 } from "lucide-react";
import { api, type Automation, type Schedule, type WatchInfo } from "../api";
import { Empty, KindBadge, StatusPill } from "../components";
import { ago, until } from "../format";
import { go, useLive, useTick } from "../hooks";

export const BROWSER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Manual / daily at a time on chosen weekdays / every N hours. */
export function ScheduleEditor({ value, onChange }: { value: Schedule | null; onChange: (s: Schedule | null) => void }) {
  const mode = value?.type ?? "manual";
  return (
    <div className="schedule-editor">
      <select
        className="input"
        value={mode}
        onChange={(e) => {
          const m = e.target.value;
          onChange(m === "daily" ? { type: "daily", time: "09:00", days: [1, 2, 3, 4, 5] } : m === "hourly" ? { type: "hourly", every: 1 } : null);
        }}
      >
        <option value="manual">Only when I click Run</option>
        <option value="daily">Every day at a set time</option>
        <option value="hourly">Every few hours</option>
      </select>
      {value?.type === "daily" && (
        <div className="row gap wrap center">
          <input className="input time" type="time" value={value.time} onChange={(e) => onChange({ ...value, time: e.target.value })} />
          <div className="days">
            {DAYS.map((d, i) => {
              const n = i + 1;
              const on = value.days.includes(n);
              return (
                <button
                  key={d}
                  type="button"
                  className={`day ${on ? "on" : ""}`}
                  onClick={() => {
                    const days = on ? value.days.filter((x) => x !== n) : [...value.days, n].sort();
                    if (days.length) onChange({ ...value, days });
                  }}
                >
                  {d}
                </button>
              );
            })}
          </div>
        </div>
      )}
      {value?.type === "hourly" && (
        <label className="row gap center small">
          every
          <input className="input narrow" type="number" min={1} max={24} value={value.every}
            onChange={(e) => onChange({ ...value, every: Math.min(24, Math.max(1, Number(e.target.value) || 1)) })} />
          hours
        </label>
      )}
      {value && <div className="muted tiny">Times are in {BROWSER_TZ}. If Fleet is off at run time, it runs once when it's back.</div>}
    </div>
  );
}

/** "Tell me when price <= 150": rules on the columns of a saved scrape step. Matches show up under Updates in the Inbox. */
function Watches({ automationId }: { automationId: string }) {
  const [info, setInfo] = useState<WatchInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [form, setForm] = useState({ step: "", column: "", op: "<=", value: "" });
  const load = () => api.watches(automationId).then((i) => { setInfo(i); setErr(null); }).catch((e) => setErr(String(e.message ?? e)));
  useEffect(() => { load(); }, [automationId]);
  if (!info || info.steps.length === 0) return err ? <div className="error small">{err}</div> : null;

  const step = info.steps.find((s) => s.key === (form.step || info.steps[0].key)) ?? info.steps[0];
  const numeric = form.value.trim() !== "" && Number.isFinite(Number(form.value.replace(",", ".")));
  async function add() {
    try {
      await api.addWatch(automationId, { step_key: step.key, column: form.column || step.columns[0] || "", op: form.op, threshold: Number(form.value.replace(",", ".")) });
      setForm({ ...form, value: "" });
      await load();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <div className="stack small">
      <strong><Bell size={13} className="inline-icon" /> Watches</strong>
      <span className="muted">Tell me when a number in a scrape crosses a line you set. You hear about rows that just crossed it, once.</span>
      {info.rules.map((r) => (
        <div className="row gap center wrap" key={r.id}>
          <span>{r.column_name} {r.op} {r.threshold}</span>
          <span className="muted">on {info.steps.find((s) => s.key === r.step_key)?.title ?? r.step_key}</span>
          <button className="link small" onClick={() => api.deleteWatch(r.id).then(load).catch((e) => setErr(String(e.message ?? e)))}>remove</button>
        </div>
      ))}
      <div className="row gap center wrap">
        {info.steps.length > 1 && (
          <select className="input" value={step.key} onChange={(e) => setForm({ ...form, step: e.target.value, column: "" })}>
            {info.steps.map((s) => <option key={s.key} value={s.key}>{s.title}</option>)}
          </select>
        )}
        {step.columns.length > 0 ? (
          <select className="input" value={form.column || step.columns[0]} onChange={(e) => setForm({ ...form, column: e.target.value })}>
            {step.columns.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        ) : (
          <input className="input" placeholder="Column, like price" value={form.column} onChange={(e) => setForm({ ...form, column: e.target.value })} />
        )}
        <select className="input" value={form.op} onChange={(e) => setForm({ ...form, op: e.target.value })}>
          {["<=", "<", ">=", ">"].map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
        <input className="input" style={{ width: 110 }} placeholder="150" value={form.value} onChange={(e) => setForm({ ...form, value: e.target.value })} />
        <button className="btn" disabled={!numeric} onClick={add}>Add watch</button>
      </div>
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

export function Automations() {
  const { data, error, reload } = useLive(api.automations, (ev) => ev.type === "task.status" || ev.type.startsWith("automation"));
  useTick(30000);
  return (
    <div className="page">
      <h1>Automations</h1>
      <p className="muted">
        An automation replays the steps of a task that worked — the same steps you already saw — so it doesn't re-plan or
        pick new accounts. Run it by hand or on a schedule. Save one from any finished task with “Save as automation”.
      </p>
      {error && <div className="error">{error}</div>}
      {data && data.length === 0 && (
        <Empty icon={Repeat} title="No automations yet">
          Run a task, check the result, then click “Save as automation” on it.
        </Empty>
      )}
      <div className="stack">
        {data?.map((a) => <AutomationCard key={a.id} a={a} onChange={reload} />)}
      </div>
    </div>
  );
}

function AutomationCard({ a, onChange }: { a: Automation; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [schedule, setSchedule] = useState<Schedule | null>(a.schedule);
  const [err, setErr] = useState<string | null>(null);

  async function call(fn: () => Promise<unknown>) {
    setErr(null);
    try {
      await fn();
      onChange();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className={`card automation ${a.enabled ? "" : "off"}`}>
      <div className="row gap center wrap">
        <label className="switch" title={a.enabled ? "Schedule on" : "Schedule paused"}>
          <input type="checkbox" checked={a.enabled} onChange={(e) => call(() => api.patchAutomation(a.id, { enabled: e.target.checked }))} />
          <span />
        </label>
        <strong className="grow-text">{a.name}</strong>
        <button className="btn primary" onClick={() => call(async () => go(`/tasks/${(await api.runAutomation(a.id)).id}`))}>
          <Play size={15} /> Run now
        </button>
        <button className="btn danger-ghost" title="Delete automation"
          onClick={() => window.confirm(`Delete “${a.name}”? Past runs stay in Tasks.`) && call(() => api.deleteAutomation(a.id))}>
          <Trash2 size={15} />
        </button>
      </div>
      <div className="row gap wrap center small">
        <span>{a.schedule_text}</span>
        {a.enabled && a.next_run_at && <span className="muted">· next {until(a.next_run_at)}</span>}
        {!a.enabled && a.schedule && <span className="muted">· paused</span>}
        <button className="link small" onClick={() => { setEditing((v) => !v); setSchedule(a.schedule); }}>
          {editing ? "close" : "change schedule"}
        </button>
      </div>
      {editing && (
        <div className="stack">
          <ScheduleEditor value={schedule} onChange={setSchedule} />
          <div className="row gap">
            <button className="btn primary" onClick={() => call(async () => {
              await api.patchAutomation(a.id, schedule ? { schedule, timezone: BROWSER_TZ } : { clear_schedule: true });
              setEditing(false);
            })}>Save schedule</button>
          </div>
        </div>
      )}
      <div className="row gap wrap center small">
        {a.last_task_id ? (
          <>
            <span className="muted">Last run {ago(a.last_run_at)}</span>
            {a.last_status && <StatusPill status={a.last_status} />}
            <button className="link small" onClick={() => go(`/tasks/${a.last_task_id}`)}>open</button>
          </>
        ) : (
          <span className="muted">Never run</span>
        )}
        <span className="muted">· budget ${a.settings.budget_usd} per run</span>
      </div>
      <label className="toggle small">
        <input type="checkbox" checked={a.settings.require_approval}
          onChange={(e) => call(() => api.patchAutomation(a.id, { require_approval: e.target.checked }))} />
        <span>
          Ask me before irreversible actions
          <span className="muted block">
            A scheduled run that needs your OK waits in the Inbox (up to 30 min, then that step fails). Payments always ask.
          </span>
        </span>
      </label>
      {a.last_status === "failed" && a.last_error && <div className="error small">{a.last_error}</div>}
      {a.steps.some((s) => s.kind === "scrape") && <Watches automationId={a.id} />}
      <button className="link small" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />} {a.steps.length} saved steps
      </button>
      {open && (
        <ol className="plan-list">
          {a.steps.map((s) => (
            <li key={s.key}>
              <div className="row gap center wrap">
                <KindBadge kind={s.kind} />
                <strong>{s.title}</strong>
                {s.depends_on.length > 0 && <span className="muted small">after {s.depends_on.join(", ")}</span>}
              </div>
              <p className="muted small pre">{s.instructions}</p>
            </li>
          ))}
        </ol>
      )}
      {err && <div className="error small">{err}</div>}
    </div>
  );
}
