-- Browser subtasks that don't name an account only run on profiles the operator marked for
-- general browsing. Account profiles are used only when a plan names them explicitly (and the
-- operator okays that plan), so a page's hidden instructions can't reach an unrelated login.
alter table profiles add column general_use boolean not null default false;
