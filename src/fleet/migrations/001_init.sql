-- AdsPower profiles Fleet is allowed to drive. Synced from AdsPower, opted in by hand.
create table profiles (
    id               uuid primary key default gen_random_uuid(),
    adspower_id      text not null unique,
    name             text not null,
    group_name       text not null default '',
    enabled          boolean not null default false,
    status           text not null default 'idle' check (status in ('idle', 'busy', 'error')),
    notes            text not null default '',   -- shown to the planner: what this account is for
    allowed_domains  text[] not null default '{}',
    lease_owner      text,
    lease_expires_at timestamptz,
    last_used_at     timestamptz,
    last_error       text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);

create table tasks (
    id               uuid primary key default gen_random_uuid(),
    prompt           text not null,
    status           text not null default 'queued' check (status in
                       ('queued', 'planning', 'running', 'reviewing', 'done', 'failed', 'cancelled')),
    plan_summary     text,
    result           text,
    error            text,
    round            int not null default 0,
    max_rounds       int not null default 3,
    max_parallel     int not null default 4,
    require_approval boolean not null default true,
    allowed_domains  text[] not null default '{}',
    input_tokens     bigint not null default 0,
    output_tokens    bigint not null default 0,
    cost_usd         numeric(12, 4) not null default 0,
    lease_owner      text,
    lease_expires_at timestamptz,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now(),
    finished_at      timestamptz
);

create table subtasks (
    id             uuid primary key default gen_random_uuid(),
    task_id        uuid not null references tasks (id) on delete cascade,
    round          int not null,
    key            text not null,
    title          text not null,
    kind           text not null check (kind in ('browser', 'fetch', 'research')),
    instructions   text not null,
    start_url      text,
    depends_on     text[] not null default '{}',
    profile_id     uuid references profiles (id) on delete set null,  -- the profile it ran on (shown in the panel)
    requested_profile_id uuid references profiles (id) on delete set null,  -- the planner's choice; null = any
    model_tier     text not null default 'fast' check (model_tier in ('fast', 'smart')),
    status         text not null default 'pending' check (status in
                     ('pending', 'queued', 'running', 'needs_human', 'done', 'failed', 'skipped', 'cancelled')),
    attempts       int not null default 0,
    max_attempts   int not null default 2,
    result         text,
    error          text,
    human_request  jsonb,
    human_response jsonb,
    worker_id      text,
    heartbeat_at   timestamptz,
    run_after      timestamptz not null default now(),
    steps          int not null default 0,
    input_tokens   bigint not null default 0,
    output_tokens  bigint not null default 0,
    cost_usd       numeric(12, 4) not null default 0,
    history_path   text,
    created_at     timestamptz not null default now(),
    started_at     timestamptz,
    finished_at    timestamptz,
    unique (task_id, round, key)
);

create index subtasks_claim_idx on subtasks (status, run_after) where status = 'queued';
create index subtasks_task_idx on subtasks (task_id);

create table events (
    id         bigserial primary key,
    task_id    uuid references tasks (id) on delete cascade,
    subtask_id uuid references subtasks (id) on delete cascade,
    ts         timestamptz not null default now(),
    level      text not null default 'info' check (level in ('debug', 'info', 'warn', 'error')),
    type       text not null,
    message    text not null default '',
    data       jsonb not null default '{}'
);

create index events_task_idx on events (task_id, id);

-- Push every new event to listeners (the API streams them to the panel).
create function notify_event() returns trigger language plpgsql as $$
begin
    perform pg_notify('fleet_events', json_build_object('id', new.id, 'task_id', new.task_id)::text);
    return new;
end;
$$;

create trigger events_notify after insert on events
    for each row execute function notify_event();

create function touch_updated_at() returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

create trigger tasks_touch before update on tasks for each row execute function touch_updated_at();
create trigger profiles_touch before update on profiles for each row execute function touch_updated_at();
