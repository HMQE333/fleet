-- Watches: "tell me when <column> <= <number>" on a saved automation's scrape step. A matching row that was NOT already matching in
-- the previous run is an alert; alerts are kept with the result and shown under Updates in the Inbox.
create table watch_rules (
    id uuid primary key default gen_random_uuid(),
    automation_id uuid not null references automations (id) on delete cascade,
    step_key text not null,
    column_name text not null,
    op text not null check (op in ('<', '<=', '>', '>=')),
    threshold double precision not null,
    created_at timestamptz not null default now(),
    unique (automation_id, step_key, column_name, op, threshold)
);

alter table scrape_results add column alerts jsonb;
