# Fleet

Type what you want done. Fleet plans it, sends workers out in parallel — some reading the web,
some watching videos, some driving your real AdsPower browser profiles — and asks you when it
needs a human: to okay a plan that touches your accounts, to get past a captcha or login, or to
approve something irreversible. When a result is good, save it as an automation and run it again
on a schedule.

Read [RULES.md](RULES.md) first: it's what the product will and won't do. [FIRST_REAL_RUNS.md](FIRST_REAL_RUNS.md) is the short
checklist for trying it with real credits and a real AdsPower profile; [ROADMAP_AUTOMATION.md](ROADMAP_AUTOMATION.md) and
[AUTONOMOUS_PROGRESS.md](AUTONOMOUS_PROGRESS.md) say what is built and what is left; [FUTURE_IDEAS.md](FUTURE_IDEAS.md) is what is parked.

```
┌───────────────────────────── Control panel (React) ─────────────────────────────┐
│ prompt · plan review · live workers + screenshots · inbox · automations ·       │
│ profiles · settings (API keys, models)                                          │
└──────────────────────────────────────┬──────────────────────────────────────────┘
                                       │ REST + live events (SSE), localhost-only, CSRF-guarded
┌──────────────────────────────────────▼──────────────────────────────────────────┐
│ FastAPI                                                                         │
│  Conductor: plan → (your OK if it uses your accounts) → workers → review → done │
│             also starts saved automations when their schedule is due            │
│  Worker pool: claims subtasks from Postgres (FOR UPDATE SKIP LOCKED)            │
│    research  Perplexity, or any model + web search       (via OpenRouter)       │
│    fetch     Scrapling → Markdown → model     (robots.txt, per-domain pacing)   │
│    scrape    a model learns a page's recipe once; code reads every page + item  │
│    crawl     one site section, no model, resumable                              │
│    video     Gemini watches the video (YouTube via Google AI Studio)            │
│    browser_fast  Jev: one small decision per step in a leased AdsPower profile  │
│    browser   browser-use over CDP → one leased AdsPower profile (full agent)    │
│  Guard: captcha / 2FA / login → pause and ask.  Vault: API keys, AES-256-GCM.   │
└──────────────────────────────────────┬──────────────────────────────────────────┘
                                       │
   Postgres: tasks · subtasks (queue) · profiles (leases) · automations · secrets · events
```

## Setup (Windows, local)

You need: [uv](https://docs.astral.sh/uv/) (or plain Python 3.12), Node 20+, and an [OpenRouter](https://openrouter.ai/keys)
API key (an [Anthropic](https://console.anthropic.com/) key is optional). Postgres is optional too: with
`FLEET_DATABASE_URL=embedded` Fleet runs its own (the `pgserver` package, under `data/pg`). AdsPower (paid plan, Local API on)
is only needed for AdsPower profiles.

```bash
cp .env.example .env        # keys, and FLEET_DATABASE_URL (embedded, or your own Postgres)
uv sync --extra embedded     # or: python -m venv .venv && .venv\Scripts\pip install -e .[embedded]
cd web && npm install && npm run build && cd ..
uv run fleet serve          # → http://127.0.0.1:8710
```

First run:
1. **Settings → API keys**: paste your OpenRouter key. It's stored encrypted and never shown again.
   **Settings → Models** picks which model does each job. A Claude key there is optional: with one, the planner and
   the readers can run on Anthropic's own API instead of OpenRouter (the `anthropic-api/…` models).
2. A browser profile: either `uv run fleet profiles add-local "Scratch"` (a Chrome / Edge profile Fleet runs itself, no
   AdsPower; add `--visible` to see its window, or press **Open** on the Profiles page when you want to log in by hand),
   or open AdsPower and **Profiles → Sync from AdsPower**.
3. Switch on the profiles Fleet may use and write a note for each ("RybiaPaka Buffer + Instagram,
   logged in"): the planner reads these to pick the right account. Turn on **general browsing**
   only for profiles that aren't logged in to anything important.
4. Click **Open** on each profile and log in by hand once. Fleet reuses the session.
5. Go to **Tasks** and type a prompt.

For panel development with hot reload: `uv run fleet serve` in one terminal and
`cd web && npm run dev` in another (http://localhost:5173, proxies to the API).

## How a task runs

1. **Plan.** The planner splits the prompt into subtasks, picks a worker for each, and sets
   dependencies. Anything without a dependency starts at once, in parallel.

   A prompt that names **one page** and what to do there skips planning entirely: it becomes a single fast-browser
   step that starts at that page with your words as its goal, and no planning model is called (`fleet/planner.py`,
   `one_page`). Anything that reaches past that page — another source, many pages, a table to collect — is planned
   as usual.

   | Worker | Does | Default model (Settings → Models) |
   |---|---|---|
   | research | web search with sources; `smart` tier = deep research | Perplexity Sonar Pro / Sonar Deep Research |
   | fetch | reads one public URL | Claude Haiku 4.5 (`fast`) / Sonnet 5 (`smart`) |
   | scrape | collects many items from a list page and the pages after it into a downloadable table | Sonnet 5 (once, to learn the recipe) |
   | crawl | collects the pages of one section of a site (docs, a blog) into a table; no model, free | none |
   | video | watches one video | Gemini 3.8 Flash |
   | browser_fast | clicks and types in an AdsPower profile, one small decision per step | TypeSafe Jev (+ Haiku 4.5 for typed values) |
   | browser | the full vision agent, for anything browser_fast can't do | Claude Sonnet 5 |
   | planner | plans and reviews | Claude Opus 5 |

   Every model runs through OpenRouter; the cost shown is what OpenRouter actually charged. The four brain
   roles (planner, smart reader, fast reader, browser agent) can run on **Anthropic's own API** instead — pick an
   `anthropic-api/…` model in Settings → Models and save a Claude key. Then the call goes straight to
   api.anthropic.com on your key and Fleet prices it from the token counts Anthropic reports (`fleet/providers.py`:
   input, output, prompt-cache writes at 1.25× and reads at a tenth), so the ledger and the budgets work the same.
   Jev, Perplexity and Gemini always stay on OpenRouter — they don't exist on the Anthropic API.
2. **Your OK.** A plan that would act in your browser profiles waits in the **Inbox** until you
   approve it (per task: when it uses profiles / always / never). Steps that don't name an account
   only run on profiles marked for general browsing.
3. **Work.** Workers claim subtasks from Postgres. A browser step also leases a profile, so two
   workers never drive the same one. Every step is logged with a screenshot and the actions taken.
4. **Hand-off.** A captcha, login wall or 2FA prompt pauses the worker and puts a card in your
   **Inbox**. Finish it in the AdsPower window and click *I've handled it*. Irreversible actions
   (sending, posting, deleting, paying) wait for your *Approve*. You stay in control of a running task: **Pause**
   it, **Take over** a browser step, approve routine clicks once with *always allow*, or answer a task's waiting
   cards together (never payments or sign-ups).
5. **Review.** The planner reads the results and writes the answer, or adds another round (up to
   `max_rounds`). The task's **budget** caps total spend. A **saved automation's** run skips this round when its steps
   answer for themselves — a single finished step, or scrape / crawl steps whose reports are the answer — so repeating
   work you already approved costs only the steps.

A finished task with a failed step has **Retry this step** on that step: only it, and the steps that were skipped because of it, run
again; everything that worked stays, and the planner reviews again at the end. A step that already did something irreversible is not
offered for retry (run the task again once you've checked what happened).

Crashes are safe: workers heartbeat, and a dead worker's subtask is retried and its profile lease
expires — except a subtask that already did something irreversible, which fails with an
explanation instead of repeating the action. Everything lives in Postgres, so a restart loses
nothing. A failed task has **Run again**.

## Scraping

A `scrape` step turns a list page (a category, a set of listings, search results) into a table. A
model looks at the page **once** and writes a *recipe*: CSS selectors for the repeating items, their
fields, and the "next page" link. Fleet's code checks the recipe against the real page (it only
trusts one that really finds several items with filled fields; otherwise the model gets one repair
round) and then reads every page itself, following the next links, removing duplicates and
stopping at the page, item and time limits (`FLEET_SCRAPE_MAX_PAGES` 10, `FLEET_SCRAPE_MAX_ITEMS`
300, `FLEET_SCRAPE_MAX_SECONDS` 300). Measured on a real 10-page site: 200 items for $0.02.

- **Saved automations reuse the recipe**: a scheduled scrape reads the pages for $0 until the site's
  layout changes, then learns the recipe again.
- **Partial data is kept**: if page 7 is blocked, you still get pages 1-6 and the reason.
- **Results** show on the step as a table preview with CSV and JSON downloads (the CSV is safe to open
  in a spreadsheet: scraped text can't run as a formula).
- **Item pages**: when the goal needs more than the list shows (a full description, specs, stock), the model also
  learns how to read one item's own page, once, checked against a real sample. Fleet then opens each item's page
  itself (same site only, paced, at most `FLEET_SCRAPE_MAX_DETAILS` = 30 per step, stops after three refusals) and
  adds the values as extra columns. Saved automations reuse that recipe for $0; if a saved item-page recipe stops
  fitting, it is learned again. If item pages can't be read, you still get the list data and the reason.
- **Updates in the Inbox**: a scheduled run that found changes shows up under Updates (what is new, removed or
  changed, with a link to the details) until you mark it as seen; runs where nothing changed stay quiet. It is news,
  not a question: nothing waits on it. (Alerts that leave this machine, like email or a webhook, are not built.)
- **Watches**: on a saved automation with a scrape step, "tell me when `price <= 150`" (Automations → Watches). Numbers are read
  from the cell text in any local format ("1 299,99 zł", "$1,299.99"). You hear about rows that **just crossed** the line, once, in
  the step's result and under Updates in the Inbox, not about the same cheap row every run.
- **Change monitoring**: when a saved automation scrapes the same page again, the step compares it with the previous run and says what is new, what disappeared and what was edited (a price drop, a new listing). Rows are matched by a unique id or link column, so a renamed product is an edit, not a new row. If a run stopped early, Fleet reports edits only, because a row that wasn't reached isn't a removed row.
- Public pages only, `robots.txt` honoured, requests paced, never local addresses. A site that blocks
  automation is a job for a browser step on your own logged-in profile, not for evasion. Lists that
  JavaScript builds are recognised up front and sent to a browser step.

## Logging in and signing up

- **Saved logins** (Settings → Site logins): Fleet's code types the login when a task reaches that site's login
  form; the AI never sees it. The Inbox asks first unless you marked the login automatic.
- **Two-step logins** (email first, password on the next page or after the form swaps) work too, as long as
  you saved a login for the site. Fleet only types into a page that clearly is the sign-in step, never into a
  newsletter box, contact or comment form.
- **Sign-ups**: tick "Allow Fleet to create an account if the task needs one" in the task options and fill in
  Settings → Your details for sign-ups. When a browser step reaches a sign-up form, the Inbox shows what Fleet
  would fill in and which terms it would tick; nothing is typed until you approve (or you can do it yourself).
  Fleet generates a strong password, saves it encrypted (Show password in Settings → Site logins) and leaves
  confirmation emails and captchas to you. Limits: one account per site and profile, 3 a day
  (`FLEET_REGISTRATIONS_PER_DAY`), and forms asking for anything Fleet can't honestly fill (phone, address...) are
  left to you.

## Staying in control of the Inbox

Everything that needs you waits in the Inbox: plans to OK, approvals, captchas, logins, sign-ups. For clicks you
approve again and again, answer **Approve and always allow** (fast browser): Fleet then clicks that exact button on
that exact site in that profile without asking, logs each use, and you can remove the rule in Settings → Trust rules.
Payments, deleting, accepting terms, sign-ups and vague buttons like "OK" always ask.

The Inbox groups waiting cards by task. When a task has several, **Approve N together** approves the ones a click would
approve anyway (a saved login, an approval that is not a payment) and **Stop all** stops them; payments, sign-ups and
captchas always need their own answer.

A task can also opt out of your "always allow" rules ("Use my always-allow rules" in the task options): then every click that needs
approval asks. And under Settings → Your details you can list sites Fleet must **never sign up on**, whatever a task allows.

On a running task, **Pause** stops new steps from starting (the ones already running finish) until you press Resume.
On a running browser step, **Take over** makes the worker wait at its next step so you can use the AdsPower window
yourself; click "Give control back" and Fleet continues from whatever the page shows (or stop the step instead).

## Crawling a section of a site

A `crawl` step collects the pages of one section of a public site (a docs site, a blog, a help centre) into a table with
the address, title, depth, word count and text of each page. It follows links by code: same site, inside the start URL's
folder, up to `FLEET_CRAWL_MAX_PAGES` = 50 pages and `FLEET_CRAWL_MAX_DEPTH` = 3 clicks, no files, no `nofollow` links,
no endless calendar or filter variants, robots.txt and pacing like every page read. It also reads the site's `sitemap.xml`
(if there is one and robots.txt allows it) for pages of the section that nothing links to. It uses no model and costs nothing.
Progress is saved every ten pages, so an interrupted crawl resumes instead of starting over. Like a scrape, the table can
be previewed and downloaded, and a saved automation compares each run with the previous one (new pages, edited text) and
shows what changed under Updates.

## Fast browser

`browser_fast` reads the page as structured text (the visible controls and their labels, no
screenshots for the model) and asks Jev — a small "pick one of these" model on OpenRouter — for
one action per step. Code executes it: only elements it actually observed, re-checked right before
the click, never password, card or one-time-code fields. Seconds and fractions of a cent per step.

Jev is only ever shown the controls the window actually holds, so when it reports that nothing on the page helps and
the page has more below, Fleet reads on (up to four screens) before giving up — a "Next" link under ten results is the
ordinary case. Fleet's own headless windows are desktop-sized (1280x1024) for the same reason. A control the window
edge cuts in half counts as held: Fleet offers it and aims at the part of it that is on screen (a search field whose
middle is a few pixels below the fold is still a search field), with the same hit test before any input. And when
nothing on a page helps and there is nothing left to read, Fleet first steps back out of branches it clicked into
itself — up to two — because the page it came from may still hold the way on; only then is the step handed over.

It reads and acts inside the page's own frames (an embedded editor or widget served from the same origin is read
with the page, its buttons offered like any other); a frame from another origin is left alone. A file input is offered
as an upload: Fleet asks you for the file (an Inbox card; write the path on this computer in the note) and sets it
without a dialog; the page and the model never choose the path, and a hidden file input behind a custom "Choose file"
button isn't seen. A link or button that opens a new tab doesn't lose it: the new tab becomes the page it reads and
acts on (checked against the allowed domains and the private-address rules like any page), "back" returns to the tab
that opened it, and every tab it opened is closed when the step ends. It can't use rich text editors; when it's stuck,
blocked or out of steps, the full browser agent takes over in the same profile (unless it already did something you
approved — that's never redone). Switch it off in **Settings → Fast browser**.
Adapted from [jev-ultrafast](https://github.com/browser-use/jev-ultrafast) and
[JevBrowser](https://github.com/digitalfoudnry-vb/JevBrowser), both MIT (`src/fleet/fastbrowser/NOTICE.md`).

## Macros

A macro is a step list the brain writes once (or you write by hand) that the hands run with no model call at all:
`navigate`, `click`, `fill`, `select`, `wait_for`, `extract`, `back`, `scroll`, `assert_text` (`fleet/macro.py`). It is
validated before a browser opens (a step that would type into a password, card or code field is refused outright) and runs
through the fast browser's page reader: an element must be on the page under that name, a click that reads like a
commitment waits for your approval, private addresses are refused. It stops honestly at the first step it can't do
(`blocked`, `stuck`, `rejected`) and its trace is the same replayable path a browser step records. Cost: $0.

## Automations

**Save as automation** on a finished task stores the steps that worked. Each run replays exactly
those steps — no re-planning, so no new decisions about which accounts to use — and writes a fresh
answer. Browser steps also keep the path they took (which elements, what was typed): replays run
them with the fast browser following that path, so a task the full agent learned once runs in
seconds afterwards — no AI call where the page still matches, Jev where it changed, the full agent
if both get stuck. A path heals itself: when the site renamed a button or a field, Jev decides that one
step, the run goes on, and the saved path is updated to the new label ("Path updated" in the activity),
so a change on the site is paid for once; a replay that was handed to the full agent or that you stopped
never rewrites the path. Schedules: daily at a time on chosen weekdays, or every N hours, in your time zone. Runs
never overlap, fire once per slot even with several conductors, and missed slots (computer off)
aren't replayed.

## Voice

Fleet understands spoken commands in Polish and English without a model: `fleet.voice.parse(text)` is a hand-written
grammar (one verb, then its object) that answers in microseconds and costs nothing. It knows: `open wikipedia.org`,
`search for raspberry pi 5` / `szukaj ...`, `click Next page` / `kliknij Zaloguj`, `open the third result` / `otwórz trzeci
wynik`, `type hello` / `wpisz ...`, `scroll down` / `przewiń w dół` / `niżej`, `go back` / `wróć`, `next tab` /
`następna karta`, `read the page` / `przeczytaj`, `enter`, `approve` / `zgoda`, `reject` / `nie`, `stop`, `help` / `pomoc`,
and `new task ...` / `zadanie ...`, which hands the rest of the sentence to the planner as an ordinary task. What it
doesn't understand returns nothing, and the panel asks again rather than guessing. The grammar's benchmark lives in
`readiness/benchmarks/voice_utterances.json` (gate V1: at least 95% right).

A spoken password, PIN, CVV or card number is never a command: `type my password hunter2` becomes `refused`, whose reason
never repeats the words (RULES.md §3 holds for the microphone as it does for the model).

A **voice session** (`fleet.voice.Session`) runs those commands on a live page over CDP, built on the fast engine's page
reader: a spoken label is matched against the labels the snapshot reads from the page (exactly, then by prefix, then by
containment), "the third result" is the third link, and a navigation command (scroll, back, click) runs in the time of
one observe and one act, under a second (gate V4), with no model call. The engine's gates hold in code: a click whose
label reads like a commitment (send, pay, delete...) is never clicked on a spoken word alone and waits for `approve` (or
`reject`); nothing is ever typed into a password or card field; a private address is refused like it is for the engine.

The API (gate V2), guarded like every other route: `POST /api/voice/task {"text"}` makes a task from transcribed words
(`new task X` means X; any other sentence is the task itself) and answers `201 {"task_id"}`; `POST /api/voice/session
{"url"}` opens a visible tab in a local Chrome profile named "Voice" (created on first use, never leased to workers) and
answers `201 {"session_id"}`; `POST /api/voice/command {"session_id", "text"}` runs one command there and answers
`{"command", "result": {"ok", "said"}}`; `DELETE /api/voice/session/{id}` closes the tab. A refused utterance (a
password, a card number) is `400` and nothing is typed; what the grammar doesn't understand is `400` too.

In the panel (gate V3) the sidebar has a **Talk** button: push to talk, the browser's own speech recognition (Web Speech
API, Chrome and Edge, no model, no cost) turns the words into text, Fleet's grammar decides what they mean, and the
answer is read back with speechSynthesis - so are a plan waiting in the Inbox ("say approve or reject"), a request for
help, and a finished task. With no page open yet, "open allegro.pl" opens the voice session there; "new task ..." makes a
task and shows it; a spoken "approve" answers the Inbox's first waiting plan unless a click on the page is what waits.
The PL / EN button picks the language of the words, the speaker button mutes the readback; both are remembered in the
browser. The words never leave the browser except to Fleet's own `/api/voice/...`.

## Command line

```bash
uv run fleet run "Compare the Raspberry Pi 5 8GB price in 5 Polish shops" --budget 1   # asks before plans that use profiles; --yes skips
uv run fleet secrets set OPENROUTER_API_KEY   # hidden prompt; same vault as the panel
uv run fleet profiles add-local "<name>" [--visible] | sync | list | enable "<name>" | note "<name>" "what it's for"
uv run fleet worker                           # extra workers on another machine that runs AdsPower
uv run fleet migrate
```

## Limits

How much one step may do (scrape pages and items, item pages, crawl pages and depth, time, accounts per day) is set in
Settings → Limits and applies to the next step, no restart. Each limit has a hard ceiling in code. The values start from the
`.env` / defaults (see `.env.example`); "Back to the default" removes your change. The pause between requests to one site,
robots.txt and the public-address checks are not editable.

## Costs

Settings → Costs shows the bill for the last 7, 30 or 90 days (`GET /api/costs/summary?days=30`): spend per role (each step kind,
plus "planner" for the part of a task's bill that no step accounts for: planning, review, summaries) and, for every saved automation
that ran in the period, its first run's cost against what its replays cost, with the difference as "saved". A negative saving means
the replays got dearer than the first run — a drifted path that fell back to the model every time, worth re-recording. Only
aggregates leave the API: no prompts, no page text.

## Notifications (optional, off by default)

Settings → Notifications: paste a webhook address (Discord, Slack, ntfy, your own), tick what you want to hear about (something needs you, a
saved automation found changes or crossed a watch line, a task finished or failed) and press "Send a test". Fleet then sends one short line
per event. Nothing leaves your computer until you do this; no passwords or keys are ever in a message; the address must be a public https one,
is stored encrypted and is never shown again (only its host); at most 20 messages an hour.

## The experience base

Every browser step, however it ends, writes one row to the `experience` table: the site, what the step was asked, the path it took
(the same replayable fill/click list a saved automation uses), the outcome and its one-line reason, cost and time. Never page text,
never a model's prose, never a secret. This is what makes the second visit to a site cheaper than the first.

The base answers two questions, by SQL and plain Python — no model is asked what a past run meant. *How did a goal like this go on
this site?* (`experience.best_path`) gives back the path of the latest step that finished there with a similar goal — similar meaning
the two goals share at least 60% of the shorter one's words once words like "the" and "please" are dropped — and nothing at all for a
different errand. *What do we know about this site?* (`experience.site_notes`) gives back one line: how many recent runs finished, how
many did not, what the last one that did not said, and whether a login wall was met. Both answers are data the caller may use as a
hint — a path to try, a note to quote to the planner — never an instruction (READINESS.md, gates K2-K5).

Both are used. A browser step that has no recorded path of its own starts with the best known one as its guide: the hands try it
and the engine heals it where the page has moved on, so only the first visit pays a model for every click. The planner is told, in
the same prompt it plans from, what Fleet knows about each site the task names (the URLs in the request, the domains it is limited
to, then the domains its profiles may visit) — under the label every block of text Fleet did not write itself carries, *data, not
instructions*. A note that quotes a page saying "ignore all rules" is quoted to the model, never obeyed.

The panel's Knowledge page lists it per site (`GET /api/experience?domain=`, `GET /api/experience/sites`) with a Forget
button on every entry (`DELETE /api/experience/{id}`): remove what was learned on a page that has since changed.

## Storage

Screenshots of browser steps, saved agent histories and the step-by-step log grow with every task. Settings → Storage shows how much
they take; under Limits, "Keep screenshots and step-by-step logs for N days" (0 = forever, the default) makes Fleet clean up finished
tasks older than that about every six hours, or at once with "Clean up now". Only those three things are removed. Answers, scraped data
(and so change monitoring and watches), saved automations and the record of your approvals and Inbox answers are kept.

## Security

- The API answers only to `localhost` (add hosts with `FLEET_ALLOWED_HOSTS`), refuses changes from
  other sites, and serves the panel without path traversal. Set `FLEET_API_TOKEN` and use HTTPS
  before exposing it anywhere else.
- API keys and saved logins: AES-256-GCM, master key in Windows Credential Manager (or `FLEET_MASTER_KEY`),
  write-only API (except passwords Fleet generated for you), scrubbed from logs.
- See RULES.md for what the agents may do.

## Tests

```bash
uv run pytest                                            # tests that need no database
FLEET_TEST_DATABASE_URL=postgresql://postgres:PW@localhost:5432/fleet_test uv run pytest   # everything
```

The test database is dropped and recreated on every run. Pipeline tests run the real conductor,
worker, queue, leases and scheduler with stand-in AI (no API spend, no browser); OpenRouter tests
use a fake OpenRouter and the provider tests a fake Anthropic API (`tests/test_providers.py`); security
tests attack the API like a hostile page would.

## Moving to the cloud later

- Postgres → any managed Postgres (Supabase, Neon, RDS).
- AdsPower's Local API only listens on its own machine, so browser workers run next to AdsPower:
  `fleet worker` there, `fleet serve --api-only` in the cloud.
- Set `FLEET_MASTER_KEY` (no OS credential store on servers), `FLEET_API_TOKEN`, `FLEET_ALLOWED_HOSTS`.

What's deliberately not built yet is in [FUTURE_IDEAS.md](FUTURE_IDEAS.md). What "ready" means and how far Fleet is
from it: [READINESS.md](READINESS.md) and `READINESS_STATUS.md`; the loop that closes the gap: [LOOP.md](LOOP.md).
