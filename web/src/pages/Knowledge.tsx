import { useEffect, useState } from "react";
import { BookOpen, Trash2 } from "lucide-react";
import { api, type ExperienceEntry, type ExperienceSite } from "../api";
import { Empty, StatusPill } from "../components";
import { ago, money } from "../format";
import { useLive } from "../hooks";

/**
 * What Fleet learned, per site: every browser step's goal, path and outcome, with a Forget button for each entry.
 * Calls GET /api/experience/sites, GET /api/experience?domain= and DELETE /api/experience/{id} (api.experienceSites,
 * api.experience, api.forgetExperience).
 */
export function Knowledge() {
  const { data: sites, error, reload } = useLive(api.experienceSites, (ev) => ev.type === "subtask.status", []);
  const [domain, setDomain] = useState<string | null>(null);
  const [entries, setEntries] = useState<ExperienceEntry[] | null>(null);
  const [entriesError, setEntriesError] = useState<string | null>(null);

  const selected = domain ?? sites?.[0]?.domain ?? null;

  useEffect(() => {
    if (!selected) {
      setEntries(null);
      return;
    }
    let live = true;
    api.experience(selected)
      .then((rows) => { if (live) { setEntries(rows); setEntriesError(null); } })
      .catch((e) => { if (live) setEntriesError(e instanceof Error ? e.message : String(e)); });
    return () => { live = false; };
  }, [selected, sites]);

  async function forget(e: ExperienceEntry) {
    try {
      await api.forgetExperience(e.id);
      setEntries((rows) => (rows ?? []).filter((r) => r.id !== e.id));
      reload();
    } catch (err) {
      setEntriesError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="page wide">
      <div className="section-head">
        <h1>Knowledge</h1>
      </div>
      <p className="muted">
        Every browser step, however it ended, leaves one entry here: the site, what the step was asked, the path it took and
        how it ended. This is what makes the second visit to a site cheaper than the first. Forget an entry when it taught
        Fleet the wrong thing (a path recorded on a page that has since changed, a goal you no longer want it to remember).
        Never page text, never a secret.
      </p>
      {error && <p className="error small">{error}</p>}
      {sites && sites.length === 0 && (
        <Empty icon={BookOpen} title="Nothing learned yet">Run a browser task and come back: every step is written down here.</Empty>
      )}
      {sites && sites.length > 0 && (
        <div className="row gap wrap small">
          {sites.map((s: ExperienceSite) => (
            <button key={s.domain} className={`btn ${s.domain === selected ? "primary" : ""}`} onClick={() => setDomain(s.domain)}
              title={`${s.succeeded} of ${s.count} ended well · ${money(s.cost_usd)} · last ${ago(s.last_at)}`}>
              {s.domain || "(no site)"} <span className="muted">{s.count}</span>
            </button>
          ))}
        </div>
      )}
      {entriesError && <p className="error small">{entriesError}</p>}
      {selected && entries && (
        <div className="stack">
          {entries.map((e) => <EntryCard key={e.id} e={e} onForget={() => forget(e)} />)}
          {entries.length === 0 && <p className="muted small">Nothing left for this site.</p>}
        </div>
      )}
    </div>
  );
}

function EntryCard({ e, onForget }: { e: ExperienceEntry; onForget: () => void }) {
  const [open, setOpen] = useState(false);
  const steps = Array.isArray(e.actions) ? e.actions : [];
  return (
    <div className="card">
      <div className="row gap wrap center">
        <StatusPill status={e.status} />
        <span className="pill">{e.kind}</span>
        <strong>{e.goal}</strong>
        <span className="spacer" />
        <span className="muted small">
          {steps.length} steps · {money(e.cost_usd)} · {Math.round(e.seconds)}s · {ago(e.created_at)}
        </span>
        <button className="btn danger-ghost" onClick={onForget} title="Remove this entry from what Fleet knows about the site">
          <Trash2 size={15} /> Forget
        </button>
      </div>
      {e.reason && <p className="muted small">{e.reason}</p>}
      {steps.length > 0 && (
        <div className="stack small">
          <button className="link small" onClick={() => setOpen(!open)}>{open ? "Hide the path" : "Show the path"}</button>
          {open && (
            <ol className="muted small">
              {steps.map((a, i) => (
                <li key={i}>
                  {a.kind}{a.label ? ` “${a.label}”` : ""}{a.value ? ` ← ${a.value}` : ""}
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
      {e.task_id && <a className="link small" href={`#/tasks/${e.task_id}`}>Open the task</a>}
    </div>
  );
}
