import { useState } from "react";
import { ClipboardCheck, Eye, Inbox as InboxIcon, TrendingUp } from "lucide-react";
import { api, type InboxItem, type InboxUpdate } from "../api";
import { Empty, HumanRequestCard } from "../components";
import { ago } from "../format";
import { go, useLive } from "../hooks";

/** News from a saved automation: what changed since its previous run. The details are on the step's data preview. */
function UpdateCard({ u, onSeen }: { u: InboxUpdate; onSeen: () => void }) {
  const c = u.counts ?? { added: 0, removed: 0, changed: 0, unchanged: 0 };
  return (
    <div className="human-card update-card">
      <div className="human-head">
        <TrendingUp size={18} />
        <div>
          <div className="human-title">{u.automation_name}: {u.alerts.length > 0 ? "watch alert" : "changes found"}</div>
          <div className="muted small">{u.title} · {u.rows} rows · {ago(u.updated_at)}</div>
        </div>
      </div>
      {u.alerts.length > 0 && (
        <ul className="change-list">
          {u.alerts.slice(0, 8).map((a, i) => (
            <li key={i} className="change-edit">
              ! {a.label}: {a.column} {a.value}{" "}
              <span className="muted">({a.op} {a.threshold}{a.was ? `, was ${a.was}` : ""})</span>
            </li>
          ))}
          {u.alerts.length > 8 && <li className="muted">…and {u.alerts.length - 8} more</li>}
        </ul>
      )}
      <div className="row gap wrap small">
        {!u.partial && c.added > 0 && <span className="pill tone-ok">+{c.added} new</span>}
        {!u.partial && c.removed > 0 && <span className="pill tone-bad">−{c.removed} removed</span>}
        {c.changed > 0 && <span className="pill tone-warn">~{c.changed} changed</span>}
        {u.partial && <span className="muted">A run stopped early, so new and removed rows weren't checked.</span>}
      </div>
      <div className="row gap">
        <button className="btn primary" onClick={() => go(`/tasks/${u.task_id}`)}>See what changed</button>
        <button className="btn" onClick={() => api.markUpdateSeen(u.id).then(onSeen)}><Eye size={15} /> Mark as seen</button>
      </div>
    </div>
  );
}

/** What a click approves anyway, so several can be approved together: a saved login, or an approval that is explicitly
 *  not a payment. Everything else (sign-ups, captchas, "I've handled it", payments) needs its own answer. */
const bulkApprovable = (item: InboxItem) =>
  item.human_request.kind === "login_saved" || (item.human_request.kind === "approval" && item.human_request.payment === false);

/** One task's waiting cards. With several, the operator may approve the eligible ones or stop them all together:
 *  only the cards shown here, as they are now. */
function TaskGroup({ items, onChanged }: { items: InboxItem[]; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const first = items[0];
  const eligible = items.filter(bulkApprovable);

  async function bulk(decision: "approve" | "reject", chosen: InboxItem[]) {
    const what = decision === "approve" ? `Approve ${chosen.length} waiting steps of this task?` : `Stop all ${chosen.length} waiting steps of this task?`;
    if (!window.confirm(what)) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.respondMany(chosen.map((i) => i.id), decision);
      if (r.skipped.length) setErr(`${r.skipped.length} could not be answered together and still wait for you.`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      onChanged();
    }
  }

  return (
    <div className="stack task-group">
      <div className="row gap center wrap">
        <button className="link small inbox-task" onClick={() => go(`/tasks/${first.task_id}`)}>
          {first.prompt.length > 110 ? first.prompt.slice(0, 110) + "…" : first.prompt}
        </button>
        {items.length > 1 && <span className="muted small">{items.length} waiting</span>}
        <span className="spacer" />
        {eligible.length > 1 && (
          <button className="btn" disabled={busy} onClick={() => bulk("approve", eligible)}
            title="Approves exactly these cards. Payments, sign-ups and captchas always need their own answer.">
            Approve {eligible.length} together
          </button>
        )}
        {items.length > 1 && (
          <button className="btn danger-ghost" disabled={busy} onClick={() => bulk("reject", items)}>Stop all {items.length}</button>
        )}
      </div>
      {err && <div className="error small">{err}</div>}
      {items.map((item) => (
        <HumanRequestCard
          key={item.id}
          subtaskId={item.id}
          request={item.human_request}
          title={item.title}
          profileId={item.profile_id}
          profileName={item.profile_name}
          onDone={onChanged}
        />
      ))}
    </div>
  );
}

export function Inbox() {
  const { data, error, reload } = useLive(
    api.inbox,
    (ev) => ev.type.startsWith("human.") || ev.type === "subtask.status" || ev.type === "task.status" || ev.type === "plan" ||
      ev.type === "scrape.changes",
  );
  const total = data ? data.plans.length + data.requests.length : 0;
  const updates = data?.updates ?? [];
  const groups = [...(data?.requests ?? []).reduce((m, item) => m.set(item.task_id, [...(m.get(item.task_id) ?? []), item]), new Map<string, InboxItem[]>()).values()];

  return (
    <div className="page">
      <div className="section-head">
        <h1>Inbox</h1>
        {total > 0 && <span className="muted small">{total} waiting</span>}
      </div>
      <p className="muted">
        Plans that would use your accounts wait here for your OK. Workers also stop here when they hit a captcha, a
        login, or an action that needs your approval — handle it in the AdsPower window, then tell the worker to continue.
      </p>
      {error && <div className="error">{error}</div>}
      {data && total === 0 && updates.length === 0 && <Empty icon={InboxIcon} title="Nothing needs you right now" />}
      {data && data.plans.length > 0 && (
        <div className="stack">
          {data.plans.map((plan) => (
            <div key={plan.id} className="human-card">
              <div className="human-head">
                <ClipboardCheck size={18} />
                <div>
                  <div className="human-title">Plan ready — waiting for your OK</div>
                  <div className="muted small">
                    {plan.subtasks} subtasks{plan.browser_subtasks ? `, ${plan.browser_subtasks} in your browser profiles` : ""}
                    {plan.round > 1 ? ` · round ${plan.round}` : ""}
                  </div>
                </div>
              </div>
              <p className="human-message">{plan.prompt.length > 200 ? plan.prompt.slice(0, 200) + "…" : plan.prompt}</p>
              <div className="row gap">
                <button className="btn primary" onClick={() => go(`/tasks/${plan.id}`)}>Review plan</button>
              </div>
            </div>
          ))}
        </div>
      )}
      {updates.length > 0 && (
        <section className="stack">
          <div className="section-head">
            <h2>Updates</h2>
            <span className="muted small">{updates.length} with changes</span>
            {updates.length > 1 && (
              <button className="link small" onClick={() => api.markAllUpdatesSeen().then(reload)}>Mark all as seen</button>
            )}
          </div>
          {updates.map((u) => <UpdateCard key={u.id} u={u} onSeen={reload} />)}
        </section>
      )}
      <div className="stack">
        {groups.map((items) => <TaskGroup key={items[0].task_id} items={items} onChanged={reload} />)}
      </div>
    </div>
  );
}
