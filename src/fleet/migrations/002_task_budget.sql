-- Spending cap per task. Once reached, no new subtasks start and the planner wraps up.
alter table tasks add column budget_usd numeric(12, 4) not null default 2.00;
