-- The experience base (READINESS.md, promise 4): what every browser step did and how it ended, per site, so Fleet knows
-- a site before it goes back. Written by both browser workers after every run, whatever the outcome.
create table experience (
    id          uuid primary key default gen_random_uuid(),
    domain      text not null,                  -- the start page's host, lower-case
    goal        text not null,                  -- the step's instructions (the operator's words, not the page's)
    kind        text not null,                  -- browser_fast | browser | macro
    status      text not null,                  -- done | goal_achieved | stuck | blocked | rejected | stopped | max_steps | timeout | error | failed
    reason      text not null default '',       -- the engine's one-line reason, never page text
    actions     jsonb not null default '[]',    -- the replayable path: [{kind, label, value}]
    cost_usd    numeric(12, 4) not null default 0,
    seconds     real not null default 0,
    task_id     uuid references tasks (id) on delete set null,
    subtask_id  uuid references subtasks (id) on delete set null,
    created_at  timestamptz not null default now()
);

create index experience_domain_idx on experience (domain, created_at desc);
