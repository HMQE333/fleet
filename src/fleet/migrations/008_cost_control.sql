-- Cost control: deep research is opt-in per task, every step carries an expected cost (the budget
-- reserves it before the step starts), and each task keeps its running estimate.
alter table tasks add column allow_deep_research boolean not null default false;
alter table tasks add column estimated_cost numeric(12, 4) not null default 0;
alter table subtasks add column expected_cost numeric(12, 4) not null default 0;
