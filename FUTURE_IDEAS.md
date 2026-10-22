# Future ideas

Parked on purpose: each is useful, but none beats what's already done for the core promise right now.

## Product
- **Teach by video**: record your screen doing a task once in AdsPower; Gemini (the video worker)
  watches it and writes the recorded path (click “Schedule”, type the caption, click “Publish”), which
  the fast browser follows. Same path format as recorded runs (`fleet/fastbrowser/trace.py`).
- **Voice mode**: speech → prompt into the same task API; read plans, approvals and results aloud;
  approve/reject by voice. The approval and Inbox flows already exist; voice is an input/output layer.
- **Fast browser in frames and pop-ups**: the page reader only sees the top document today.
- **Event triggers** ("when X happens, do Y"): a webhook endpoint per automation first (simple, deterministic,
  signed with a per-automation secret), then email / RSS polling. Schedules already cover time-based cases.
- **Edit saved steps** of an automation (instructions, order) instead of re-saving from a new run.
- **Recipes for browser steps** (done as recorded paths + fast browser; see README): browser-use can also replay a saved history without the LLM
  (`Agent.rerun_history`). Histories are stored in `data/histories/`; replaying them for automation runs would
  cut cost and flakiness. Needs a fallback to the agent when the page changed.
- **Custom API tools**: let the operator store extra keys in the vault and expose them to workers through a
  server-side HTTP tool (the model names the key; the server injects it; the model never sees it).
- **Cost estimate on plan review** (from the model catalog prices and typical token counts).

## Done since this list was written
- Retry one failed step, retention of old screenshots / logs, outbound notifications (webhook), price watches, crawl, editable limits: see ROADMAP_AUTOMATION.md.

## Platform
- **Accounts and tenants**: user login, `org_id` on every table, per-org vault master keys.
- **Indexes for scale**: partial index on `subtasks (task_id) where status = 'pending'`; the conductor scans
  pending work every second.
- **Packaging**: Dockerfile for the API, installer for the AdsPower-side worker, CI running `pytest` and
  `npm run build`.
- **Observability**: per-model cost dashboard, error tracking.

## Known limits
- The captcha detector is heuristic (visible reCAPTCHA / hCaptcha / Turnstile / Arkose / DataDome frames,
  Cloudflare interstitials, common phrases, OTP inputs). A missed challenge surfaces as the agent calling
  `request_human` or failing the step.
- Side-effect tracking only covers actions the agent declares through `request_approval`.
