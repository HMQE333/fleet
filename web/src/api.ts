export type TaskStatus =
  | "queued" | "planning" | "awaiting_approval" | "running" | "reviewing" | "done" | "failed" | "cancelled";
export type PlanReview = "auto" | "always" | "never";
export type SubtaskStatus =
  | "pending" | "queued" | "running" | "needs_human" | "done" | "failed" | "skipped" | "cancelled";
export type Kind = "browser" | "browser_fast" | "fetch" | "research" | "video" | "scrape" | "crawl";
export const isBrowser = (k: Kind) => k === "browser" || k === "browser_fast";

export interface Task {
  id: string;
  prompt: string;
  status: TaskStatus;
  plan_summary: string | null;
  result: string | null;
  error: string | null;
  round: number;
  max_rounds: number;
  max_parallel: number;
  require_approval: boolean;
  plan_review: PlanReview;
  allowed_domains: string[];
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  budget_usd: number;
  estimated_cost: number;
  allow_deep_research: boolean;
  allow_registration: boolean;
  use_trust_rules: boolean;
  paused: boolean;
  created_at: string;
  finished_at: string | null;
  automation_id: string | null;
}

export interface TaskSummary {
  id: string;
  prompt: string;
  status: TaskStatus;
  paused: boolean;
  round: number;
  cost_usd: number;
  created_at: string;
  finished_at: string | null;
  error: string | null;
  subtasks: number;
  done: number;
  failed: number;
  running: number;
  needs_human: number;
}

export interface HumanRequest {
  kind: "challenge" | "login" | "login_saved" | "register" | "verify_email" | "takeover" | "approval" | "help";
  message: string;
  url: string | null;
  requested_at: string;
  /** An approval the operator may answer with "always allow this" (never payments, deleting, terms). */
  offer?: { domain: string; label: string };
  /** Approval cards: whether the action is a payment or purchase (those are never approved in bulk). */
  payment?: boolean;
}

export interface Subtask {
  id: string;
  task_id: string;
  round: number;
  key: string;
  title: string;
  kind: Kind;
  instructions: string;
  start_url: string | null;
  depends_on: string[];
  profile_id: string | null;
  profile_name: string | null;
  model_tier: "fast" | "smart";
  status: SubtaskStatus;
  side_effects?: boolean;
  attempts: number;
  result: string | null;
  error: string | null;
  human_request: HumanRequest | null;
  human_response: { decision: string; note: string } | null;
  steps: number;
  cost_usd: number;
  expected_cost: number;
  data_count: number | null;
  data_changes: ChangeCounts | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface FleetEvent {
  id: number;
  task_id: string | null;
  subtask_id: string | null;
  ts: string;
  level: "debug" | "info" | "warn" | "error";
  type: string;
  message: string;
  data: Record<string, any>;
}

export interface Profile {
  id: string;
  adspower_id: string;
  /** "adspower" (synced from AdsPower) or "local" (a Chrome / Edge profile Fleet runs itself). */
  kind: "adspower" | "local";
  headless: boolean;
  name: string;
  group_name: string;
  enabled: boolean;
  general_use: boolean;
  status: "idle" | "busy" | "error";
  notes: string;
  allowed_domains: string[];
  last_used_at: string | null;
  last_error: string | null;
}

export type Schedule =
  | { type: "daily"; time: string; days: number[] }
  | { type: "hourly"; every: number };

export interface AutomationStep {
  key: string;
  title: string;
  kind: Kind;
  instructions: string;
  start_url: string | null;
  depends_on: string[];
  profile_id: string | null;
  model_tier: "fast" | "smart";
}

export interface Automation {
  id: string;
  name: string;
  prompt: string;
  steps: AutomationStep[];
  settings: { max_parallel: number; require_approval: boolean; allowed_domains: string[]; budget_usd: number };
  schedule: Schedule | null;
  schedule_text: string;
  timezone: string;
  enabled: boolean;
  next_run_at: string | null;
  last_run_at: string | null;
  last_task_id: string | null;
  last_status?: TaskStatus | null;
  last_error?: string | null;
}

export interface InboxPlan {
  id: string;
  prompt: string;
  plan_summary: string | null;
  round: number;
  browser_subtasks: number;
  subtasks: number;
}

export interface InboxItem {
  id: string;
  task_id: string;
  title: string;
  kind: Kind;
  human_request: HumanRequest;
  prompt: string;
  profile_id: string | null;
  profile_name: string | null;
}

export interface WatchAlert {
  rule_id: string;
  column: string;
  op: "<" | "<=" | ">" | ">=";
  threshold: number;
  label: string;
  value: string | null;
  was: string | null;
}

export interface WatchRule {
  id: string;
  step_key: string;
  column_name: string;
  op: "<" | "<=" | ">" | ">=";
  threshold: number;
}

export interface WatchInfo {
  rules: WatchRule[];
  steps: { key: string; title: string; columns: string[] }[];
}

/** News, not a question: a saved automation's scrape that found changes (see fleet/changes.py). */
export interface InboxUpdate {
  id: string;
  task_id: string;
  title: string;
  automation_id: string;
  automation_name: string;
  counts: ChangeCounts | null;
  alerts: WatchAlert[];
  partial: boolean;
  rows: number;
  updated_at: string;
}

export interface SiteAccount {
  id: string;
  domain: string;
  profile_id: string | null;
  profile_name: string | null;
  label: string;
  username: string;
  origin: "operator" | "fleet";
  auto_login: boolean;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  last_failed_at: string | null;
}

export interface TrustRule {
  id: string;
  profile_id: string;
  profile_name: string;
  domain: string;
  label: string;
  uses: number;
  last_used_at: string | null;
  created_at: string;
}

export interface LimitRow {
  name: string;
  label: string;
  value: number;
  default: number;
  min: number;
  max: number;
  overridden: boolean;
}

export interface StorageUsage {
  screenshots_bytes: number;
  histories_bytes: number;
  step_events: number;
  retention_days: number;
}

export interface ExperienceEntry {
  id: string;
  domain: string;
  goal: string; // the step's instructions, in the operator's words
  kind: Kind | "macro";
  status: string; // done | goal_achieved | stuck | blocked | rejected | stopped | max_steps | timeout | error | failed
  reason: string;
  actions: { kind: string; label?: string; value?: string }[]; // the replayable path
  cost_usd: number;
  seconds: number;
  task_id: string | null;
  subtask_id: string | null;
  created_at: string;
}

export interface ExperienceSite {
  domain: string;
  count: number;
  succeeded: number;
  cost_usd: number;
  last_at: string;
}

export interface CostRole {
  role: string; // a step kind, or "planner" for the part of the bill no step accounts for
  usd: number;
  steps: number;
}

export interface CostAutomation {
  automation_id: string;
  name: string;
  source_task_id: string | null;
  first_run_usd: number;
  replays: number;
  replay_total_usd: number;
  saved_usd: number; // replays × first_run_usd − replay_total_usd; negative when replays got dearer
}

export interface CostSummary {
  days: number;
  since: string;
  total_usd: number;
  tasks: number;
  by_role: CostRole[];
  automations: CostAutomation[];
}

export interface NotifyConfig {
  configured: boolean;
  host: string | null;
  on: string[];
  format: "json" | "plain";
  triggers: { name: string; label: string }[];
}

export interface Identity {
  first_name: string;
  last_name: string;
  email: string;
  username: string;
}

export interface ChangeCounts {
  added: number;
  removed: number;
  changed: number;
  unchanged: number;
}

/** What differs from the previous run of the same saved-automation step (see fleet/changes.py). */
export interface ScrapeChanges {
  comparable: boolean;
  reason?: string;
  key?: string | null;
  partial?: boolean;
  truncated?: boolean;
  counts?: ChangeCounts;
  added?: Record<string, string | null>[];
  removed?: Record<string, string | null>[];
  changed?: { key: string; label: string; changes: Record<string, { from: string | null; to: string | null }> }[];
  fields_changed?: Record<string, number>;
}

export interface ScrapeData {
  fields: string[];
  count: number;
  pages: number;
  reused: boolean;
  more_pages: boolean;
  changes: ScrapeChanges | null;
  items: Record<string, string | null>[];
}

export interface Health {
  db: boolean;
  adspower: boolean;
  llm_key: boolean;
  vault_error: string | null;
  models: Record<string, string>;
}

export interface SecretInfo {
  name: string;
  label: string;
  required: boolean;
  help: string;
  set: boolean;
  hint?: string;
  updated_at?: string;
  last_used_at?: string | null;
}

export interface ModelRole {
  role: string;
  label: string;
  help: string;
  default: string;
  needs: string[];
  offcatalog: boolean;
  model: string;
  providers: string[]; // "openrouter", and "anthropic-api" for the roles that may use your own Claude key
}

export interface CatalogModel {
  id: string;
  name: string;
  context: number | null;
  input_per_m: number;
  output_per_m: number;
  capabilities: string[];
}

export interface KeyCheck {
  ok: boolean;
  error?: string;
  label?: string;
  limit?: number | null;
  limit_remaining?: number | null;
  usage?: number;
}

const TOKEN_KEY = "fleet.token";

export function exportUrl(subtaskId: string, format: "csv" | "json"): string {
  const t = token();
  return `/api/subtasks/${subtaskId}/export.${format}${t ? `?token=${encodeURIComponent(t)}` : ""}`;
}

export function fileUrl(path: string): string {
  const t = token();
  return `/files/${path}${t ? `?token=${encodeURIComponent(t)}` : ""}`;
}

function token(): string {
  try {
    return localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function setToken(value: string) {
  try {
    localStorage.setItem(TOKEN_KEY, value);
  } catch {
    /* storage unavailable */
  }
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // X-Fleet-Client marks requests from the panel; the server refuses changes without it (CSRF guard).
  const headers: Record<string, string> = { "X-Fleet-Client": "panel" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const t = token();
  if (t) headers["Authorization"] = `Bearer ${t}`;
  const url = path.startsWith("/api/") ? path : `/api${path}`; // the voice calls name their endpoints in full
  const res = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export interface VoiceResult {
  ok: boolean;
  said: string;
  took?: number;
  needs_approval?: boolean;
  label?: string;
  text?: string;
  task_id?: string;
}

export const api = {
  health: () => request<Health>("GET", "/health"),
  // Voice (gate V2): words become a task or a command against a live tab; the server's grammar decides, not the panel.
  voiceTask: (text: string) => request<{ task_id: string; prompt: string; said: string }>("POST", "/api/voice/task", { text }),
  voiceSession: (url: string) => request<{ session_id: string; profile_id: string; url: string }>("POST", "/api/voice/session", { url }),
  voiceCommand: (session_id: string | null, text: string) =>
    request<{ command: Record<string, unknown>; result: VoiceResult }>("POST", "/api/voice/command", { session_id, text }),
  closeVoiceSession: (id: string) => request<{ ok: true }>("DELETE", `/api/voice/session/${id}`),
  tasks: () => request<TaskSummary[]>("GET", "/tasks"),
  task: (id: string) => request<{ task: Task; subtasks: Subtask[] }>("GET", `/tasks/${id}`),
  events: (id: string, after = 0) => request<FleetEvent[]>("GET", `/tasks/${id}/events?after=${after}`),
  createTask: (body: {
    prompt: string;
    max_parallel: number;
    max_rounds: number;
    require_approval: boolean;
    allowed_domains: string[];
    budget_usd: number;
    plan_review: PlanReview;
    allow_deep_research: boolean;
    allow_registration: boolean;
    use_trust_rules: boolean;
  }) => request<Task>("POST", "/tasks", body),
  approveTask: (id: string) => request<{ ok: true }>("POST", `/tasks/${id}/approve`),
  rerunTask: (id: string) => request<Task>("POST", `/tasks/${id}/rerun`),
  automations: () => request<Automation[]>("GET", "/automations"),
  saveAutomation: (body: { task_id: string; name: string; schedule: Schedule | null; timezone: string }) =>
    request<Automation>("POST", "/automations", body),
  patchAutomation: (id: string, body: { name?: string; enabled?: boolean; schedule?: Schedule; clear_schedule?: boolean; timezone?: string; require_approval?: boolean }) =>
    request<Automation>("PATCH", `/automations/${id}`, body),
  deleteAutomation: (id: string) => request<{ ok: true }>("DELETE", `/automations/${id}`),
  runAutomation: (id: string) => request<Task>("POST", `/automations/${id}/run`),
  cancelTask: (id: string) => request<{ ok: true }>("POST", `/tasks/${id}/cancel`),
  retryStep: (subtaskId: string) => request<{ retried: string; also_restarted: number }>("POST", `/subtasks/${subtaskId}/retry`),
  pauseTask: (id: string) => request<{ ok: true }>("POST", `/tasks/${id}/pause`),
  resumeTask: (id: string) => request<{ ok: true }>("POST", `/tasks/${id}/resume`),
  takeOver: (subtaskId: string) => request<{ ok: true }>("POST", `/subtasks/${subtaskId}/take-over`),
  inbox: () => request<{ plans: InboxPlan[]; requests: InboxItem[]; updates: InboxUpdate[] }>("GET", "/inbox"),
  markUpdateSeen: (subtaskId: string) => request<{ ok: true }>("POST", `/subtasks/${subtaskId}/seen`),
  markAllUpdatesSeen: () => request<{ ok: true }>("POST", "/inbox/updates/seen"),
  respond: (subtaskId: string, decision: "done" | "approve" | "reject", note = "", remember = false) =>
    request<{ ok: true }>("POST", `/subtasks/${subtaskId}/respond`, { decision, note, remember }),
  respondMany: (ids: string[], decision: "approve" | "reject", note = "") =>
    request<{ answered: string[]; skipped: { id: string; reason: string }[] }>("POST", "/inbox/respond-many", { ids, decision, note }),
  watches: (automationId: string) => request<WatchInfo>("GET", `/automations/${automationId}/watches`),
  addWatch: (automationId: string, body: { step_key: string; column: string; op: string; threshold: number }) =>
    request<WatchRule>("POST", `/automations/${automationId}/watches`, body),
  deleteWatch: (ruleId: string) => request<{ ok: true }>("DELETE", `/watches/${ruleId}`),
  noRegister: () => request<string[]>("GET", "/settings/no-register"),
  addNoRegister: (domain: string) => request<string[]>("POST", "/settings/no-register", { domain }),
  removeNoRegister: (domain: string) => request<string[]>("DELETE", `/settings/no-register/${encodeURIComponent(domain)}`),
  trustRules: () => request<TrustRule[]>("GET", "/trust-rules"),
  deleteTrustRule: (id: string) => request<{ ok: true }>("DELETE", `/trust-rules/${id}`),
  profiles: () => request<Profile[]>("GET", "/profiles"),
  syncProfiles: () => request<{ seen: number; added: number }>("POST", "/profiles/sync"),
  addLocalProfile: (body: { name: string; headless?: boolean; general_use?: boolean; notes?: string }) =>
    request<Profile>("POST", "/profiles/local", body),
  patchProfile: (id: string, body: Partial<Pick<Profile, "enabled" | "general_use" | "notes" | "allowed_domains">> & { reset?: boolean }) =>
    request<Profile>("PATCH", `/profiles/${id}`, body),
  openProfile: (id: string) => request<{ ok: true }>("POST", `/profiles/${id}/open`),
  scrapeData: (subtaskId: string, limit = 50) =>
    request<ScrapeData>("GET", `/subtasks/${subtaskId}/data?limit=${limit}`),
  siteAccounts: () => request<SiteAccount[]>("GET", "/site-accounts"),
  createSiteAccount: (body: {
    domain: string;
    username: string;
    password: string;
    profile_id: string | null;
    auto_login: boolean;
  }) => request<SiteAccount>("POST", "/site-accounts", body),
  patchSiteAccount: (id: string, body: { password?: string; username?: string; auto_login?: boolean }) =>
    request<SiteAccount>("PATCH", `/site-accounts/${id}`, body),
  deleteSiteAccount: (id: string) => request<{ ok: true }>("DELETE", `/site-accounts/${id}`),
  revealSiteAccount: (id: string) => request<{ password: string }>("POST", `/site-accounts/${id}/reveal`),
  notifications: () => request<NotifyConfig>("GET", "/settings/notifications"),
  saveNotifications: (body: { url?: string | null; on: string[]; format: "json" | "plain" }) =>
    request<NotifyConfig>("PUT", "/settings/notifications", body),
  testNotification: () => request<{ ok: true }>("POST", "/settings/notifications/test"),
  storageUsage: () => request<StorageUsage>("GET", "/maintenance/usage"),
  costSummary: (days: number) => request<CostSummary>("GET", `/costs/summary?days=${days}`),
  experienceSites: () => request<ExperienceSite[]>("GET", "/experience/sites"),
  experience: (domain: string) => request<ExperienceEntry[]>("GET", `/experience?domain=${encodeURIComponent(domain)}`),
  forgetExperience: (id: string) => request<{ ok: true }>("DELETE", `/experience/${id}`),
  pruneNow: () => request<{ tasks: number; files: number; bytes: number; events: number }>("POST", "/maintenance/prune"),
  limits: () => request<LimitRow[]>("GET", "/settings/limits"),
  saveLimits: (values: Record<string, number | null>) => request<LimitRow[]>("PUT", "/settings/limits", { values }),
  identity: () => request<Identity>("GET", "/settings/identity"),
  saveIdentity: (body: Identity) => request<Identity>("PUT", "/settings/identity", body),
  secrets: () => request<SecretInfo[]>("GET", "/secrets"),
  putSecret: (name: string, value: string) => request<SecretInfo>("PUT", `/secrets/${name}`, { value }),
  deleteSecret: (name: string) => request<{ ok: true }>("DELETE", `/secrets/${name}`),
  checkOpenRouter: () => request<KeyCheck>("POST", "/secrets/OPENROUTER_API_KEY/check"),
  modelRoles: () => request<ModelRole[]>("GET", "/settings/models"),
  saveModels: (changes: Record<string, string>) => request<ModelRole[]>("PUT", "/settings/models", changes),
  catalog: () => request<CatalogModel[]>("GET", "/models"),
  fastBrowser: () => request<{ enabled: boolean }>("GET", "/settings/fast-browser"),
  setFastBrowser: (enabled: boolean) => request<{ enabled: boolean }>("PUT", "/settings/fast-browser", { enabled }),
};

// ---- live events: one EventSource for the whole app ------------------------------------

type Listener = (ev: FleetEvent) => void;
const listeners = new Set<Listener>();
let source: EventSource | null = null;
let connected = false;
const connectionListeners = new Set<(ok: boolean) => void>();

function ensureStream() {
  if (source) return;
  const t = token();
  source = new EventSource(`/api/stream${t ? `?token=${encodeURIComponent(t)}` : ""}`);
  source.onopen = () => setConnected(true);
  source.onerror = () => setConnected(false);
  source.onmessage = (msg) => {
    try {
      const ev = JSON.parse(msg.data) as FleetEvent;
      listeners.forEach((l) => l(ev));
    } catch {
      /* ignore malformed */
    }
  };
}

function setConnected(ok: boolean) {
  if (ok === connected) return;
  connected = ok;
  connectionListeners.forEach((l) => l(ok));
}

export function onEvent(listener: Listener): () => void {
  ensureStream();
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function onConnection(listener: (ok: boolean) => void): () => void {
  ensureStream();
  connectionListeners.add(listener);
  listener(connected);
  return () => connectionListeners.delete(listener);
}
