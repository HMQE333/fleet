-- Fast browser (Jev): a second browser worker kind, plus recorded paths so a step done once by
-- the full agent can be replayed by the fast engine.
alter table subtasks drop constraint subtasks_kind_check;
alter table subtasks add constraint subtasks_kind_check
    check (kind in ('browser', 'browser_fast', 'fetch', 'research', 'video'));

-- What a browser step actually did: [{kind, label, value}] (filled by both browser engines).
alter table subtasks add column trace jsonb;
-- Recorded path to follow on this run (set when an automation replays a saved browser step).
alter table subtasks add column guide jsonb;
