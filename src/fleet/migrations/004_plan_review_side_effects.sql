-- A plan can wait for the operator's OK before any worker starts.
alter table tasks drop constraint tasks_status_check;
alter table tasks add constraint tasks_status_check check (status in
    ('queued', 'planning', 'awaiting_approval', 'running', 'reviewing', 'done', 'failed', 'cancelled'));

-- auto = ask when the plan uses browser profiles (your accounts); always; never.
alter table tasks add column plan_review text not null default 'auto' check (plan_review in ('auto', 'always', 'never'));

-- Set once a subtask has done something irreversible (an approved action).
-- Such a subtask is never retried automatically, so the action can't happen twice.
alter table subtasks add column side_effects boolean not null default false;
