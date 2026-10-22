import { useState } from "react";
import { ArrowUp, Hand, KeyRound, ListTodo, Settings2 } from "lucide-react";
import { api, type PlanReview, type TaskSummary } from "../api";
import { Empty, StatusPill } from "../components";
import { ago, money } from "../format";
import { go, useLive, useTick } from "../hooks";

const EXAMPLES = [
  "Compare the price of the Raspberry Pi 5 8GB across 5 Polish shops and tell me the cheapest with shipping.",
  "Open my Buffer dashboard and list every post scheduled for next week.",
  "Find 10 small fishing YouTube channels from Poland that posted in the last month.",
  "Summarize this video and list the gear it recommends: https://www.youtube.com/watch?v=…",
];

export function Tasks({ needsKey }: { needsKey: boolean }) {
  const { data: tasks, error } = useLive(api.tasks, (ev) => ev.type === "task.status" || ev.type === "plan" || ev.type === "subtask.status");
  useTick(30000);

  return (
    <div className="page">
      {needsKey && (
        <div className="setup-banner">
          <KeyRound size={18} />
          <div>
            <strong>Add your OpenRouter key to start.</strong>
            <div className="muted small">One key runs every model — Claude, Gemini, Perplexity and more.</div>
          </div>
          <span className="spacer" />
          <button className="btn primary" onClick={() => go("/settings")}>Open Settings</button>
        </div>
      )}
      <Composer disabled={needsKey} />
      <section>
        <div className="section-head">
          <h2>Tasks</h2>
          {tasks && <span className="muted small">{tasks.length} total</span>}
        </div>
        {error && <div className="error">{error}</div>}
        {tasks && tasks.length === 0 && (
          <Empty icon={ListTodo} title="No tasks yet">
            Describe what you want done above. Fleet plans it, sends workers out in parallel, and asks you when it needs a hand.
          </Empty>
        )}
        <div className="task-list">
          {tasks?.map((t) => <TaskRow key={t.id} t={t} />)}
        </div>
      </section>
    </div>
  );
}

function TaskRow({ t }: { t: TaskSummary }) {
  const pct = t.subtasks ? Math.round(((t.done + t.failed) / t.subtasks) * 100) : 0;
  return (
    <button className="task-row" onClick={() => go(`/tasks/${t.id}`)}>
      <div className="task-row-main">
        <div className="task-row-prompt">{t.prompt}</div>
        <div className="task-row-meta">
          <StatusPill status={t.status} />
          {t.paused && t.status === "running" && <span className="pill tone-neutral">Paused</span>}
          {t.needs_human > 0 && (
            <span className="pill tone-warn">
              <Hand size={12} /> {t.needs_human} waiting on you
            </span>
          )}
          {t.subtasks > 0 && (
            <span className="muted small">
              {t.done}/{t.subtasks} subtasks{t.failed ? ` · ${t.failed} failed` : ""}
            </span>
          )}
          {t.status === "failed" && t.error && <span className="error small clamp1">{t.error}</span>}
        </div>
      </div>
      <div className="task-row-side">
        {t.subtasks > 0 && (
          <div className="progress">
            <div className="progress-bar" style={{ width: `${pct}%` }} />
          </div>
        )}
        <div className="muted small">
          {money(t.cost_usd)} · {ago(t.created_at)}
        </div>
      </div>
    </button>
  );
}

function Composer({ disabled }: { disabled: boolean }) {
  const [prompt, setPrompt] = useState("");
  const [parallel, setParallel] = useState(4);
  const [rounds, setRounds] = useState(3);
  const [approval, setApproval] = useState(true);
  const [budget, setBudget] = useState(2);
  const [review, setReview] = useState<PlanReview>("auto");
  const [deep, setDeep] = useState(false);
  const [register, setRegister] = useState(false);
  const [useRules, setUseRules] = useState(true);
  const [domains, setDomains] = useState("");
  const [showOptions, setShowOptions] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function submit() {
    if (prompt.trim().length < 3 || busy || disabled) return;
    setBusy(true);
    setErr(null);
    try {
      const task = await api.createTask({
        prompt: prompt.trim(),
        max_parallel: parallel,
        max_rounds: rounds,
        require_approval: approval,
        allowed_domains: domains.split(/[\s,]+/).filter(Boolean),
        budget_usd: budget,
        plan_review: review,
        allow_deep_research: deep,
        allow_registration: register,
        use_trust_rules: useRules,
      });
      setPrompt("");
      go(`/tasks/${task.id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="composer">
      <h1>What should Fleet do?</h1>
      <div className="composer-box">
        <textarea
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit();
          }}
          placeholder={EXAMPLES[0]}
          rows={3}
        />
        <div className="composer-bar">
          <button className="btn ghost small-btn" onClick={() => setShowOptions((v) => !v)}>
            <Settings2 size={15} /> Options
          </button>
          <span className="muted small hide-narrow">
            {parallel} workers · up to ${budget}{deep ? " · deep research on" : ""}{register ? " · may sign up (you approve)" : ""} · {approval ? "asks before irreversible actions" : "approval off"}
          </span>
          <span className="spacer" />
          <span className="muted small hide-narrow">Ctrl+Enter</span>
          <button className="btn primary round" disabled={busy || disabled || prompt.trim().length < 3} onClick={submit} aria-label="Run" title={disabled ? "Add your OpenRouter key in Settings first" : "Run"}>
            <ArrowUp size={18} />
          </button>
        </div>
        {showOptions && (
          <div className="composer-options">
            <label className="field">
              <span>Parallel workers</span>
              <input type="number" className="input" min={1} max={32} value={parallel} onChange={(e) => setParallel(Number(e.target.value) || 1)} />
            </label>
            <label className="field">
              <span>Max planning rounds</span>
              <input type="number" className="input" min={1} max={6} value={rounds} onChange={(e) => setRounds(Number(e.target.value) || 1)} />
            </label>
            <label className="field">
              <span>Budget (USD)</span>
              <input type="number" className="input" min={0.1} max={500} step={0.5} value={budget} onChange={(e) => setBudget(Number(e.target.value) || 0.1)} />
            </label>
            <label className="field">
              <span>Only these domains (optional)</span>
              <input className="input" placeholder="allegro.pl, x-kom.pl" value={domains} onChange={(e) => setDomains(e.target.value)} />
            </label>
            <label className="field wide">
              <span>Show me the plan before it runs</span>
              <select className="input" value={review} onChange={(e) => setReview(e.target.value as PlanReview)}>
                <option value="auto">When it would use my browser profiles (recommended)</option>
                <option value="always">Always</option>
                <option value="never">Never — start right away</option>
              </select>
            </label>
            <label className="toggle wide">
              <input type="checkbox" checked={deep} onChange={(e) => setDeep(e.target.checked)} />
              <span>
                Allow deep research
                <span className="muted small block">
                  Long multi-source reports. About $1.50 and several minutes per step, so it's off unless you need it.
                </span>
              </span>
            </label>
            <label className="toggle wide">
              <input type="checkbox" checked={register} onChange={(e) => setRegister(e.target.checked)} />
              <span>
                Allow Fleet to create an account if the task needs one
                <span className="muted small block">
                  Uses your details from Settings. Every sign-up still waits for your OK in the Inbox, and
                  confirmation emails and captchas are yours.
                </span>
              </span>
            </label>
            <label className="toggle wide">
              <input type="checkbox" checked={useRules} onChange={(e) => setUseRules(e.target.checked)} />
              <span>
                Use my “always allow” rules
                <span className="muted small block">Off: every click that needs approval asks, even where you said “always allow”.</span>
              </span>
            </label>
            <label className="toggle wide">
              <input type="checkbox" checked={approval} onChange={(e) => setApproval(e.target.checked)} />
              <span>
                Ask me before irreversible actions
                <span className="muted small block">Sending, posting, deleting, accepting terms. Payments always ask.</span>
              </span>
            </label>
          </div>
        )}
      </div>
      <div className="examples">
        {EXAMPLES.map((ex) => (
          <button key={ex} className="chip" onClick={() => setPrompt(ex)}>
            {ex}
          </button>
        ))}
      </div>
      {err && <div className="error">{err}</div>}
    </section>
  );
}
