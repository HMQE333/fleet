# First real runs: what to try once you have credits and AdsPower

Everything in Fleet is tested with stand-ins (a fake model, a headless browser, local test sites): hundreds of tests. What those can't tell you is
whether a *real model* picks the right worker and writes good selectors, and whether *real sites and a real AdsPower profile* behave like the
test pages. This is the shortest path to find out, cheapest first. Each step says what to do, what good looks like, what it costs, and what to
write down if it goes wrong. Stop whenever something looks off; nothing here needs more than a few dollars in total.

Before you start: `uv run fleet serve`, open <http://127.0.0.1:8710>, **Settings → API keys** (OpenRouter key). Keep **Settings → Limits** at the
defaults. For the browser steps, open AdsPower and **Profiles → Sync**, switch on ONE profile for general browsing that is not logged in to
anything important.

## 1. The model answers (about $0.02)

*Task:* `What is the capital of Poland? Answer in one sentence.`
*Good:* the plan has one cheap research step (no browser), a one-line answer, the cost shown is a few cents at most.
*If not:* note the plan (the task page shows it) and the error. A "wrong format" error means the planner model can't follow the schema:
try another model under **Settings → Models**.

## 2. Does the planner pick `scrape` and learn a recipe? (about $0.03)

*Task:* `Collect the title, price and rating of the books on the first 3 pages of https://books.toscrape.com/catalogue/category/books/mystery_3/index.html as a table.`
*Good:* a `scrape` step (not one fetch per page), a table of ~60 rows with title/price/rating columns, "Learned how to read the page" in the
activity, cost around $0.02. Download the CSV and open it: the first cell of a text starting with `=` must be shown as text.
*If not:* write down which worker the planner chose and the recipe notes line. If it chose `fetch` ×3, the `scrape` description in
`fleet/prompts.py` (PLANNER_SYSTEM) needs sharpening.

## 3. Item pages (about $0.05)

*Task:* the same, plus `...and for each book also the description and the availability from its own page.`
*Good:* the list recipe names the link field, "Learned how to read an item page", extra columns `description` / `availability`, at most 30 item
pages opened (`FLEET_SCRAPE_MAX_DETAILS`), "Opened N item pages of M" in the result.
*If not:* the model didn't set `detail_link_field`: look at the list recipe in the step's activity and at `SCRAPE_SYSTEM`.

## 4. Save it, run it again, see the changes (about $0.00 the second time)

On the finished task press **Save as automation**, then **Run now**. *Good:* the second run costs $0 ("Used the saved recipe", no model call), says
"No changes since the previous run", and the Updates section of the Inbox stays empty. Then add a **Watch** (`price <= 15`, Automations → Watches)
and run again: rows already below stay quiet.
*If not:* a "recipe no longer fits" message on an unchanged site means selectors were too brittle.

## 5. Crawl a docs section (free)

*Task:* `Collect the pages of https://docs.python.org/3/tutorial/ into a table.` (or any docs section you like)
*Good:* a `crawl` step, up to 50 pages inside `/3/tutorial/`, 3 clicks deep, no model cost, a table with url / title / words / text.
*If not:* write down whether the planner chose `crawl` at all (the prompt text is the only thing steering it).

## 6. A browser step with AdsPower (about $0.05 fast, $0.60 full)

*Task:* `Open https://www.wikipedia.org, search for "Gdańsk", open the article and tell me its population.` with the profile from the start.
*Good:* a `browser_fast` step; screenshots in the activity; a correct answer; when it works, **Take over** during the run pauses it and
"Give control back" continues. Try **Pause** on a task with several steps.
*Also try:* the same task with **Settings → Fast browser** switched off (the full agent): note the cost, and check **Take over** works there too
(that hook was never run against the real agent).

## 7. A login you save (no cost beyond step 6)

Pick a site where you have an account you don't mind testing with (ideally one with a normal password login, not a big provider with 2FA).
**Settings → Site logins → Add a login** (leave "automatic" OFF). *Task:* `Open <the site's login page> and tell me the name shown after login.`
*Good:* an Inbox card "Use your saved login?", approve, the name comes back, and the password is nowhere: not in the activity, not in the
task page, not in any answer. For a two-step site (email first, password next) the same card appears on the email page.
*If not:* write down the site and what the login page looks like (one field or two) so the detection can be adjusted.

## 8. A sign-up (only if you really want an account)

**Settings → Your details**: your real name and email. Run a task with **Allow Fleet to create an account** on, pointing at a sign-up page you
actually want. *Good:* an Inbox card listing exactly what will be filled and which terms get ticked; nothing typed before you approve; the
generated password readable in **Site logins → Show password** afterwards. Do NOT try this on a site you don't want an account on.

## 9. Notifications (only if you want them)

**Settings → Notifications**: paste an https webhook (a private Discord channel, ntfy topic...), tick one thing, **Send a test**. *Good:* one line
arrives; saving a `http://` address is refused; the address is never shown again (only its host).

## Already run for real (2026-10-09, about $2.30 in total)

Steps 1-6 were run with a real OpenRouter key, the real models and a headless Edge standing in for AdsPower (details and the defects they found:
`AUTONOMOUS_PROGRESS.md`, loop run 22). Measured: fast browser on Wikipedia $0.055 for the whole task, the full agent on the same task $0.22,
a 2-page scrape $0.08 and its saved rerun $0, a 17-page docs crawl $0.06, Polish research with sources $0.12; take-over, approvals and the captcha
hand-over behaved on both browsers. Still yours to try, because they need your own accounts or AdsPower itself: step 7 (a saved login on a site you
use), step 8 (a sign-up), step 9 (notifications), and the browser steps with a real AdsPower profile rather than the stand-in.

Two things you may run into: a site that hides its login form inside an `aria-hidden` container (Facebook does) isn't auto-filled, Fleet asks you to
log in by hand there; and some big sites show no form at all to a headless browser, which a real AdsPower profile does not suffer from.

## What to send back

For each step: worked / didn't, the cost shown on the task, and for anything that didn't work the task page's activity and the exact wording
of the error. That is enough to fix the prompts or the page detection without guessing.
