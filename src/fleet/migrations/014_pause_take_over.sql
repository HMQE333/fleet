-- Operator control over running work: pause a task (running steps finish, no new steps start) and take over a
-- running browser step (the worker waits at its next step until the operator gives control back).
alter table tasks add column paused boolean not null default false;
alter table subtasks add column take_over boolean not null default false;
