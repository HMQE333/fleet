-- Two last pieces of operator control:
--  * a task can ignore the operator's "always allow" rules (every click that needs approval asks);
--  * sites Fleet must never sign up on, whatever a task allows.
alter table tasks add column use_trust_rules boolean not null default true;

create table no_register_domains (
    domain text primary key,
    created_at timestamptz not null default now()
);
