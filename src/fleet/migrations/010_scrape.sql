-- Structured scraping: a new worker kind, and a table for what it collected. The items live apart from
-- subtasks so task polling stays light; the recipe is kept so saved automations can re-run the scrape
-- without asking a model again.
alter table subtasks drop constraint subtasks_kind_check;
alter table subtasks add constraint subtasks_kind_check
    check (kind in ('browser', 'browser_fast', 'fetch', 'research', 'video', 'scrape'));

create table scrape_results (
    subtask_id uuid primary key references subtasks (id) on delete cascade,
    fields     jsonb not null,          -- column names, in order
    items      jsonb not null,          -- [{field: value, ...}, ...]
    recipe     jsonb,                   -- the selectors that worked (see fleet/scrape.py)
    pages      int not null default 0,
    reused     boolean not null default false,
    more_pages boolean not null default false,
    updated_at timestamptz not null default now()
);
