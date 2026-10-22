-- A saved automation = the steps of a task that worked (already seen and approved by the operator)
-- + its settings + an optional schedule. Runs replay the steps without re-planning.
create table automations (
    id             uuid primary key default gen_random_uuid(),
    name           text not null,
    prompt         text not null,
    steps          jsonb not null,               -- [{key, title, kind, instructions, start_url, depends_on, profile_id, model_tier}]
    settings       jsonb not null default '{}',  -- max_parallel, require_approval, allowed_domains, budget_usd
    schedule       jsonb,                        -- null = manual only (see fleet/schedules.py)
    timezone       text not null default 'UTC',
    enabled        boolean not null default true,
    next_run_at    timestamptz,
    last_run_at    timestamptz,
    last_task_id   uuid references tasks (id) on delete set null,
    source_task_id uuid references tasks (id) on delete set null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

create index automations_due_idx on automations (next_run_at) where enabled;
create trigger automations_touch before update on automations for each row execute function touch_updated_at();

alter table tasks add column automation_id uuid references automations (id) on delete set null;
