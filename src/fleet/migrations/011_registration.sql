-- Registration: a task can allow Fleet to create an account on a site that requires one (each sign-up is
-- still approved in the Inbox). Off by default; never copied into saved automations (accounts are made once).
alter table tasks add column allow_registration boolean not null default false;
