import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowLeft, ChevronDown, ChevronRight, CircleStop, ClipboardCheck, Download, Hand, Image, Link2, Pause, Play, Repeat, RotateCw, Sparkles } from "lucide-react";
import { api, exportUrl, fileUrl, isBrowser, onEvent, type FleetEvent, type Schedule, type ScrapeChanges, type ScrapeData, type Subtask, type Task } from "../api";
import { BROWSER_TZ, ScheduleEditor } from "./Automations";
import { HumanRequestCard, KindBadge, Markdown, StatusPill } from "../components";
import { clock, duration, money } from "../format";
import { go, useLive, useTick } from "../hooks";

const ACTIVE = new Set(["queued", "planning", "awaiting_approval", "running", "reviewing"]);

export function TaskDetail({ id }: { id: string }) {
  const { data, error, reload } = useLive(() => api.task(id), (ev) => ev.task_id === id && ev.type !== "step" && ev.type !== "log", [id]);
  const events = useTaskEvents(id);
  const [focus, setFocus] = useState<string | null>(null);
  const [lightbox, setLightbox] = useState<string | null>(null);
  useTick(1000);

  const lastShot = useMemo(() => {
    const m = new Map<string, string>();
    for (const ev of events) if (ev.subtask_id && ev.data?.screenshot) m.set(ev.subtask_id, ev.data.screenshot);
    return m;
  }, [events]);

  if (error) return <div className="page"><div className="error">{error}</div></div>;
  if (!data) return <div className="page"><div className="muted">Loading…</div></div>;
  const { task, subtasks } = data;
  const waiting = subtasks.filter((s) => s.status === "needs_human" && s.human_request && !s.human_response);
  const rounds = [...new Set(subtasks.map((s) => s.round))].sort();
  const live = ACTIVE.has(task.status);

  return (
    <div className="page wide">
      <button className="back" onClick={() => go("/")}>
        <ArrowLeft size={15} /> Tasks
      </button>

      <header className="task-head">
        <h1 className="task-title">{task.prompt}</h1>
        <div className="row gap wrap center">
          <StatusPill status={task.status} />
          {task.round > 0 && <span className="muted small">Round {task.round} of {task.max_rounds}</span>}
          <span className="muted small" title="Spent so far (what OpenRouter charged) / estimate / budget">
            {money(task.cost_usd)} spent · est. {money(Number(task.estimated_cost) + 0.15)} · budget {money(task.budget_usd)}
          </span>
          <span className="muted small">{duration(task.created_at, task.finished_at)}</span>
          <span className="muted small">{task.max_parallel} workers max</span>
          {task.allowed_domains.length > 0 && <span className="muted small">Only: {task.allowed_domains.join(", ")}</span>}
          <span className="spacer" />
          {task.status === "running" && (
            task.paused ? (
              <button className="btn primary" onClick={async () => { await api.resumeTask(task.id); reload(); }}>
                <Play size={15} /> Resume
              </button>
            ) : (
              <button className="btn" onClick={async () => { await api.pauseTask(task.id); reload(); }}
                title="Steps already running finish; nothing new starts until you resume">
                <Pause size={15} /> Pause
              </button>
            )
          )}
          {live ? (
            <button className="btn danger-ghost" onClick={async () => { await api.cancelTask(task.id); reload(); }}>
              <CircleStop size={15} /> Stop task
            </button>
          ) : (
            <button className="btn" onClick={async () => go(`/tasks/${(await api.rerunTask(task.id)).id}`)}
              title="Start a new task with the same prompt and settings">
              <RotateCw size={15} /> Run again
            </button>
          )}
        </div>
      </header>

      {task.paused && task.status === "running" && (
        <div className="notice">
          <Pause size={14} className="inline-icon" /> Paused. Steps that are already running will finish, but nothing new
          starts until you press Resume.
        </div>
      )}

      {task.status === "awaiting_approval" && (
        <PlanReviewCard
          subtasks={subtasks.filter((s) => s.round === task.round)}
          estimate={subtasks.filter((s) => s.round === task.round).reduce((sum, s) => sum + Number(s.expected_cost), 0) + 0.15}
          budgetLeft={Number(task.budget_usd) - Number(task.cost_usd)}
          onRun={async () => { await api.approveTask(task.id); reload(); }}
          onCancel={async () => { await api.cancelTask(task.id); reload(); }}
        />
      )}

      {waiting.length > 0 && (
        <section className="stack">
          {waiting.map((s) => (
            <HumanRequestCard
              key={s.id}
              subtaskId={s.id}
              request={s.human_request!}
              title={s.title}
              profileId={s.profile_id}
              profileName={s.profile_name}
              onDone={reload}
            />
          ))}
        </section>
      )}

      {task.result && (task.status === "done" || task.status === "failed") && (
        <section className={`card ${task.status === "done" ? "result" : ""}`}>
          <div className="card-label">
            <Sparkles size={14} /> {task.status === "done" ? "Result" : "What the workers found"}
          </div>
          <Markdown>{task.result}</Markdown>
        </section>
      )}
      {task.automation_id && (
        <p className="muted small">
          <Repeat size={12} className="inline-icon" /> Run of a saved automation.{" "}
          <button className="link small" onClick={() => go("/automations")}>Automations</button>
        </p>
      )}
      {task.status === "done" && !task.automation_id && subtasks.some((s) => s.status === "done") && (
        <SaveAsAutomation task={task} okSteps={subtasks.filter((s) => s.status === "done").length} />
      )}
      {task.status === "failed" && task.error && (
        <section className="card bad-card">
          <div className="card-label"><AlertTriangle size={14} /> Failed</div>
          <p>{task.error}</p>
          <p className="muted small">Fix the cause if it's on your side (key, credits, profile), then use “Run again”.</p>
        </section>
      )}
      {task.plan_summary && <p className="plan-summary">{task.plan_summary}</p>}

      <div className="detail-grid">
        <section>
          <div className="section-head">
            <h2>Workers</h2>
            <span className="muted small">{subtasks.length} subtasks</span>
          </div>
          {subtasks.length === 0 && <div className="muted">{task.status === "planning" || task.status === "queued" ? "Planning…" : "No subtasks."}</div>}
          {rounds.map((r) => (
            <div key={r} className="round">
              {rounds.length > 1 && <div className="round-label">Round {r}</div>}
              {subtasks.filter((s) => s.round === r).map((s) => (
                <SubtaskCard
                  key={s.id}
                  s={s}
                  shot={lastShot.get(s.id)}
                  focused={focus === s.id}
                  onFocus={() => setFocus(focus === s.id ? null : s.id)}
                  onShot={setLightbox}
                  onRetry={
                    s.status === "failed" && (task.status === "done" || task.status === "failed") && s.round === task.round && !s.side_effects
                      ? async () => { await api.retryStep(s.id); reload(); }
                      : undefined
                  }
                />
              ))}
            </div>
          ))}
        </section>
        <ActivityFeed events={events} subtasks={subtasks} focus={focus} onClearFocus={() => setFocus(null)} onShot={setLightbox} />
      </div>

      {lightbox && (
        <div className="lightbox" onClick={() => setLightbox(null)}>
          <img src={fileUrl(lightbox)} alt="Browser screenshot" />
        </div>
      )}
    </div>
  );
}

function SaveAsAutomation({ task, okSteps }: { task: Task; okSteps: number }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(task.prompt.slice(0, 60));
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [err, setErr] = useState<string | null>(null);
  if (!open) {
    return (
      <div>
        <button className="btn" onClick={() => setOpen(true)}>
          <Repeat size={15} /> Save as automation
        </button>
      </div>
    );
  }
  return (
    <section className="card stack">
      <div className="card-label"><Repeat size={14} /> Save as automation</div>
      <p className="muted small">
        Saves the {okSteps} step{okSteps === 1 ? "" : "s"} that worked. Each run replays them — no re-planning, no new
        accounts — and writes a fresh answer.
      </p>
      <label className="field">
        <span>Name</span>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={120} />
      </label>
      <label className="field">
        <span>When should it run?</span>
        <ScheduleEditor value={schedule} onChange={setSchedule} />
      </label>
      <div className="row gap">
        <button className="btn primary" onClick={async () => {
          setErr(null);
          try {
            await api.saveAutomation({ task_id: task.id, name, schedule, timezone: BROWSER_TZ });
            go("/automations");
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e));
          }
        }}>Save automation</button>
        <button className="btn ghost" onClick={() => setOpen(false)}>Cancel</button>
      </div>
      {err && <div className="error small">{err}</div>}
    </section>
  );
}

function PlanReviewCard({ subtasks, estimate, budgetLeft, onRun, onCancel }: {
  subtasks: Subtask[]; estimate: number; budgetLeft: number; onRun: () => Promise<void>; onCancel: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const browser = subtasks.filter((s) => isBrowser(s.kind));
  const act = (fn: () => Promise<void>) => async () => {
    setBusy(true);
    setErr(null);
    try {
      await fn();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="human-card">
      <div className="human-head">
        <ClipboardCheck size={18} />
        <div>
          <div className="human-title">Review the plan before it runs</div>
          <div className="muted small">
            {browser.length
              ? `${browser.length} step${browser.length > 1 ? "s" : ""} will act in your browser profiles, so nothing starts until you say so.`
              : estimate >= 1
                ? "This plan would cost a dollar or more, so it waits for your OK."
                : "You asked to see plans first."}
          </div>
          <div className="small">
            Estimated cost: <strong>{money(estimate)}</strong> · {money(Math.max(budgetLeft, 0))} left in the budget
            {estimate > budgetLeft && <span className="error"> — some steps will be skipped to stay within budget</span>}
          </div>
        </div>
      </div>
      <ol className="plan-list">
        {subtasks.map((s) => (
          <li key={s.id}>
            <div className="row gap center wrap">
              <KindBadge kind={s.kind} />
              <strong>{s.title}</strong>
              {s.profile_name && <span className="pill tone-accent">profile “{s.profile_name}”</span>}
              {isBrowser(s.kind) && !s.profile_name && <span className="muted small">a general-browsing profile</span>}
              {s.depends_on.length > 0 && <span className="muted small">after {s.depends_on.join(", ")}</span>}
              <span className="muted small">~{money(s.expected_cost)}</span>
            </div>
            <p className="muted small pre">{s.instructions}</p>
          </li>
        ))}
      </ol>
      <div className="row gap wrap">
        <button className="btn primary" disabled={busy} onClick={act(onRun)}>
          <Play size={15} /> Run this plan
        </button>
        <button className="btn danger-ghost" disabled={busy} onClick={act(onCancel)}>
          <CircleStop size={15} /> Cancel task
        </button>
      </div>
      {err && <div className="error small">{err}</div>}
    </section>
  );
}

function useTaskEvents(id: string): FleetEvent[] {
  const [events, setEvents] = useState<FleetEvent[]>([]);
  useEffect(() => {
    let cancelled = false;
    let lastId = 0;
    const seen = new Set<number>();
    const add = (evs: FleetEvent[]) => {
      const fresh = evs.filter((e) => !seen.has(e.id));
      fresh.forEach((e) => { seen.add(e.id); lastId = Math.max(lastId, e.id); });
      if (fresh.length) setEvents((prev) => [...prev, ...fresh].sort((a, b) => a.id - b.id).slice(-2000));
    };
    setEvents([]);
    api.events(id).then((evs) => !cancelled && add(evs)).catch(() => {});
    const off = onEvent((ev) => { if (ev.task_id === id) add([ev]); });
    const poll = window.setInterval(() => api.events(id, lastId).then((evs) => !cancelled && add(evs)).catch(() => {}), 10000);
    return () => { cancelled = true; off(); window.clearInterval(poll); };
  }, [id]);
  return events;
}

/** "3 new · 1 removed · 2 changed" since the previous run, or why there is no comparison. Plain text only. */
function ChangesBlock({ changes, firstField }: { changes: ScrapeChanges; firstField: string }) {
  if (!changes.comparable || !changes.counts) {
    return <div className="muted small">Not compared with the previous run: {changes.reason}</div>;
  }
  const c = changes.counts;
  const none = !c.added && !c.removed && !c.changed;
  const name = (row: Record<string, string | null>) => row[firstField] ?? Object.values(row).find(Boolean) ?? "";
  return (
    <div className="changes small">
      <strong>
        {none
          ? `No changes since the previous run (${c.unchanged} rows the same)`
          : changes.partial
            ? `${c.changed} changed since the previous run`
            : `${c.added} new · ${c.removed} removed · ${c.changed} changed since the previous run`}
      </strong>
      {changes.partial && <div className="muted">A run stopped early, so new and removed rows weren't checked.</div>}
      {!changes.key && !none && <div className="muted">No unique column to match rows by: an edit shows as one removed and one added row.</div>}
      <ul className="change-list">
        {changes.added?.map((r, i) => <li key={`a${i}`} className="change-add">+ {name(r)}</li>)}
        {changes.removed?.map((r, i) => <li key={`r${i}`} className="change-remove">− {name(r)}</li>)}
        {changes.changed?.map((ch) => (
          <li key={ch.key} className="change-edit">
            ~ {ch.label || ch.key}:{" "}
            {Object.entries(ch.changes).map(([f, v]) => `${f} ${v.from ?? "—"} → ${v.to ?? "—"}`).join("; ")}
          </li>
        ))}
      </ul>
      {changes.truncated && <div className="muted">Showing the first 50 of each list.</div>}
    </div>
  );
}

/** What a scrape step collected: row count, a preview table, and downloads. Cells are shown as plain text. */
function DataPanel({ s }: { s: Subtask }) {
  const [data, setData] = useState<ScrapeData | null>(null);
  const [open, setOpen] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function toggle() {
    if (!open && !data) {
      try {
        setData(await api.scrapeData(s.id, 50));
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
        return;
      }
    }
    setOpen(!open);
  }

  return (
    <div className="data-panel">
      <div className="row gap center wrap small">
        <strong>{s.data_count} rows collected</strong>
        {s.data_changes && (
          <span className={`pill ${s.data_changes.added || s.data_changes.removed || s.data_changes.changed ? "tone-warn" : "tone-neutral"}`}>
            {s.data_changes.added || s.data_changes.removed || s.data_changes.changed
              ? `${s.data_changes.added} new · ${s.data_changes.removed} removed · ${s.data_changes.changed} changed`
              : "no changes"}
          </span>
        )}
        <button className="link small" onClick={toggle}>{open ? "Hide preview" : "Preview"}</button>
        <a className="link small" href={exportUrl(s.id, "csv")}><Download size={12} /> CSV</a>
        <a className="link small" href={exportUrl(s.id, "json")}><Download size={12} /> JSON</a>
      </div>
      {err && <div className="error small">{err}</div>}
      {open && data?.changes && <ChangesBlock changes={data.changes} firstField={data.fields[0]} />}
      {open && data && (
        <div className="data-table">
          <table>
            <thead><tr>{data.fields.map((f) => <th key={f}>{f}</th>)}</tr></thead>
            <tbody>
              {data.items.map((row, i) => (
                <tr key={i}>{data.fields.map((f) => <td key={f} title={row[f] ?? ""}>{row[f] ?? ""}</td>)}</tr>
              ))}
            </tbody>
          </table>
          {data.count > data.items.length && (
            <div className="muted small">Showing the first {data.items.length} of {data.count} rows. Download the CSV for all of them.</div>
          )}
          {data.more_pages && <div className="muted small">The site has more pages than the scrape limit allows.</div>}
        </div>
      )}
    </div>
  );
}

function SubtaskCard({ s, shot, focused, onFocus, onShot, onRetry }: {
  s: Subtask; shot?: string; focused: boolean; onFocus: () => void; onShot: (p: string) => void; onRetry?: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [retryErr, setRetryErr] = useState<string | null>(null);
  return (
    <div className={`subtask ${focused ? "focused" : ""} st-${s.status}`}>
      <div className="subtask-head" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
        <KindBadge kind={s.kind} />
        <span className="subtask-title">{s.title}</span>
        <StatusPill status={s.status} />
      </div>
      <div className="subtask-meta">
        {s.profile_name && <span>Profile “{s.profile_name}”</span>}
        {isBrowser(s.kind) && s.steps > 0 && <span>{s.steps} steps</span>}
        {s.started_at && <span>{duration(s.started_at, s.finished_at)}</span>}
        {Number(s.cost_usd) > 0 && <span>{money(s.cost_usd)}</span>}
        {s.attempts > 1 && <span>attempt {s.attempts}</span>}
        {s.depends_on.length > 0 && <span><Link2 size={11} /> after {s.depends_on.join(", ")}</span>}
        <span className="spacer" />
        {s.status === "running" && isBrowser(s.kind) && (
          <button className="link small" onClick={() => api.takeOver(s.id).catch(() => {})}
            title="The worker pauses at its next step so you can use the browser window yourself, then you give control back">
            <Hand size={11} /> Take over
          </button>
        )}
        <button className={`link small ${focused ? "active" : ""}`} onClick={onFocus}>
          {focused ? "Show all activity" : "Only this worker's activity"}
        </button>
      </div>
      {shot && (s.status === "running" || s.status === "needs_human") && (
        <button className="live-shot" onClick={() => onShot(shot)} title="Latest screenshot">
          <img src={fileUrl(shot)} alt="Latest browser screenshot" loading="lazy" />
        </button>
      )}
      {s.error && s.status !== "done" && <div className="error small">{s.error}</div>}
      {onRetry && (
        <div className="row gap center small">
          <button className="btn" title="Runs only this step again, and the steps that were skipped because of it. Everything that worked stays."
            onClick={() => onRetry().catch((e) => setRetryErr(e instanceof Error ? e.message : String(e)))}>
            <RotateCw size={14} /> Retry this step
          </button>
          {retryErr && <span className="error">{retryErr}</span>}
        </div>
      )}
      {!!s.data_count && <DataPanel s={s} />}
      {open && (
        <div className="subtask-body">
          <div className="card-label">Instructions</div>
          <p className="pre">{s.instructions}</p>
          {s.start_url && <p className="small"><a href={s.start_url} target="_blank" rel="noreferrer noopener">{s.start_url}</a></p>}
          {s.result && (
            <>
              <div className="card-label">Result</div>
              <Markdown>{s.result}</Markdown>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ActivityFeed({ events, subtasks, focus, onClearFocus, onShot }: {
  events: FleetEvent[]; subtasks: Subtask[]; focus: string | null; onClearFocus: () => void; onShot: (p: string) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const titles = useMemo(() => new Map(subtasks.map((s) => [s.id, s.title])), [subtasks]);
  const shown = focus ? events.filter((e) => e.subtask_id === focus) : events;

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [shown.length]);

  return (
    <section className="feed-wrap">
      <div className="section-head">
        <h2>Activity</h2>
        {focus && <button className="link small" onClick={onClearFocus}>Showing one worker · show all</button>}
      </div>
      <div
        className="feed"
        ref={box}
        onScroll={(e) => {
          const el = e.currentTarget;
          stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        {shown.length === 0 && <div className="muted small pad">Nothing yet.</div>}
        {shown.map((ev) => (
          <div key={ev.id} className={`ev ev-${ev.level} ev-type-${ev.type.replace(".", "-")}`}>
            <span className="ev-time">{clock(ev.ts)}</span>
            <div className="ev-body">
              {ev.subtask_id && !focus && <span className="ev-who">{titles.get(ev.subtask_id) ?? "worker"}</span>}
              <span className="ev-msg">
                {ev.type === "human.request" && <Hand size={12} />} {ev.message}
              </span>
              {ev.type === "step" && Array.isArray(ev.data?.actions) && ev.data.actions.length > 0 && (
                <span className="ev-actions">
                  {ev.data.actions.map((a: Record<string, unknown>, i: number) => (
                    <code key={i} title={JSON.stringify(a)}>{Object.keys(a)[0]}</code>
                  ))}
                </span>
              )}
              {ev.type === "step" && ev.data?.url && <span className="ev-url">{ev.data.url}</span>}
              {ev.data?.screenshot && (
                <button className="ev-shot" onClick={() => onShot(ev.data.screenshot)}>
                  <Image size={12} /> screenshot
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
