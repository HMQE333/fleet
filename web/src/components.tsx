import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { AppWindow, Check, Clapperboard, ExternalLink, FileText, Globe, Hand, KeyRound, MailCheck, Network, ShieldAlert, ShieldCheck, Table2, UserPlus, X, Zap } from "lucide-react";
import { api, type HumanRequest, type Kind } from "./api";

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  planning: "Planning",
  awaiting_approval: "Needs your OK",
  running: "Running",
  reviewing: "Reviewing",
  done: "Done",
  failed: "Failed",
  cancelled: "Cancelled",
  pending: "Waiting",
  needs_human: "Needs you",
  skipped: "Skipped",
};

const STATUS_TONE: Record<string, string> = {
  queued: "neutral",
  pending: "neutral",
  planning: "accent",
  awaiting_approval: "warn",
  reviewing: "accent",
  running: "info",
  needs_human: "warn",
  done: "ok",
  failed: "bad",
  skipped: "neutral",
  cancelled: "neutral",
};

const LIVE = new Set(["planning", "reviewing", "running", "needs_human", "awaiting_approval"]);

export function StatusPill({ status }: { status: string }) {
  return (
    <span className={`pill tone-${STATUS_TONE[status] ?? "neutral"}`}>
      {LIVE.has(status) && <span className="pulse" />}
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

const KIND: Record<Kind, { icon: typeof Globe; label: string }> = {
  research: { icon: Globe, label: "Research" },
  fetch: { icon: FileText, label: "Fetch" },
  browser: { icon: AppWindow, label: "Browser" },
  browser_fast: { icon: Zap, label: "Fast browser" },
  video: { icon: Clapperboard, label: "Video" },
  scrape: { icon: Table2, label: "Scrape" },
  crawl: { icon: Network, label: "Crawl" },
};

export function KindBadge({ kind }: { kind: Kind }) {
  const { icon: Icon, label } = KIND[kind];
  return (
    <span className={`kind kind-${kind}`}>
      <Icon size={13} strokeWidth={2.2} />
      {label}
    </span>
  );
}

export function Markdown({ children }: { children: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" /> }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}

const REQUEST_COPY: Record<HumanRequest["kind"], { title: string; icon: typeof Hand }> = {
  challenge: { title: "Verification needed", icon: ShieldAlert },
  login: { title: "Login needed", icon: Hand },
  login_saved: { title: "Use your saved login?", icon: KeyRound },
  register: { title: "Create an account?", icon: UserPlus },
  verify_email: { title: "Confirm your email", icon: MailCheck },
  takeover: { title: "You took over", icon: Hand },
  help: { title: "The worker needs you", icon: Hand },
  approval: { title: "Approve this action?", icon: ShieldAlert },
};

/** The operator's side of a paused subtask: approve / reject, or "I've handled it". */
export function HumanRequestCard({
  subtaskId,
  request,
  title,
  profileId,
  profileName,
  onDone,
}: {
  subtaskId: string;
  request: HumanRequest;
  title: string;
  profileId?: string | null;
  profileName?: string | null;
  onDone?: () => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const copy = REQUEST_COPY[request.kind] ?? REQUEST_COPY.help;
  const Icon = copy.icon;
  const isApproval = request.kind === "approval";
  const isSavedLogin = request.kind === "login_saved";
  const isRegister = request.kind === "register";

  async function answer(decision: "done" | "approve" | "reject", remember = false) {
    setBusy(true);
    setErr(null);
    try {
      await api.respond(subtaskId, decision, note.trim(), remember);
      onDone?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function openWindow() {
    if (!profileId) return;
    try {
      await api.openProfile(profileId);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="human-card">
      <div className="human-head">
        <Icon size={18} />
        <div>
          <div className="human-title">
            {copy.title}
            {request.payment && <span className="pill tone-bad payment-pill" title="Payments always need your own answer">Payment</span>}
          </div>
          <div className="muted small">
            {title}
            {profileName ? ` · profile “${profileName}”` : ""}
          </div>
        </div>
      </div>
      <p className="human-message">{request.message}</p>
      {request.url && (
        <a className="small url" href={request.url} target="_blank" rel="noreferrer noopener">
          {request.url.length > 90 ? request.url.slice(0, 90) + "…" : request.url}
        </a>
      )}
      <input
        className="input"
        placeholder={isApproval ? "Optional note to the worker (e.g. change the subject line)" : "Optional note to the worker"}
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <div className="row gap wrap">
        {isSavedLogin || isRegister ? (
          <>
            <button className="btn primary" disabled={busy} onClick={() => answer("approve")}>
              {isRegister ? <UserPlus size={15} /> : <KeyRound size={15} />}{}
              {isRegister ? "Create the account" : "Log in with the saved login"}
            </button>
            {profileId && (
              <button className="btn" onClick={openWindow}>
                <ExternalLink size={15} /> Open profile window
              </button>
            )}
            <button className="btn" disabled={busy} onClick={() => answer("done")}>
              I'll do it myself — continue
            </button>
            <button className="btn danger-ghost" disabled={busy} onClick={() => answer("reject")}>
              <X size={15} /> Stop this step
            </button>
          </>
        ) : isApproval ? (
          <>
            <button className="btn primary" disabled={busy} onClick={() => answer("approve")}>
              <Check size={15} /> Approve
            </button>
            {request.offer && (
              <button
                className="btn"
                disabled={busy}
                title="Fleet will click this without asking next time, only for this exact button on this site in this profile. Remove it any time in Settings → Trust rules."
                onClick={() => answer("approve", true)}
              >
                <ShieldCheck size={15} /> Approve and always allow “{request.offer.label}” on {request.offer.domain}
              </button>
            )}
            <button className="btn danger-ghost" disabled={busy} onClick={() => answer("reject")}>
              <X size={15} /> Reject
            </button>
          </>
        ) : (
          <>
            {profileId && (
              <button className="btn" onClick={openWindow}>
                <ExternalLink size={15} /> Open profile window
              </button>
            )}
            <button className="btn primary" disabled={busy} onClick={() => answer("done")}>
              <Check size={15} /> {request.kind === "takeover" ? "Give control back — continue" : "I've handled it — continue"}
            </button>
            <button className="btn danger-ghost" disabled={busy} onClick={() => answer("reject")}>
              <X size={15} /> Stop this subtask
            </button>
          </>
        )}
      </div>
      {err && <div className="error small">{err}</div>}
    </div>
  );
}

export function Empty({ icon: Icon, title, children }: { icon: typeof Globe; title: string; children?: React.ReactNode }) {
  return (
    <div className="empty">
      <Icon size={28} strokeWidth={1.6} />
      <div className="empty-title">{title}</div>
      {children && <div className="muted">{children}</div>}
    </div>
  );
}
