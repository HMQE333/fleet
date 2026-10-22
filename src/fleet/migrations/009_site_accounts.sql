-- Saved logins: the operator's own username + password for a site, optionally tied to one AdsPower
-- profile (each profile is one real identity). The password is encrypted like API keys (vault);
-- nothing returns it through the API, and AI models never see it.
create table site_accounts (
    id             uuid primary key default gen_random_uuid(),
    domain         text not null,                       -- e.g. example.com; matches its subdomains
    profile_id     uuid references profiles (id) on delete cascade,   -- null = any profile
    label          text not null default '',
    username       text not null,
    ciphertext     bytea not null,
    nonce          bytea not null,
    key_id         text not null,
    origin         text not null default 'operator' check (origin in ('operator', 'fleet')),
    auto_login     boolean not null default false,      -- use without asking in the Inbox
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    last_used_at   timestamptz,
    last_failed_at timestamptz
);

create unique index site_accounts_one_per_profile on site_accounts (domain, profile_id) where profile_id is not null;
create unique index site_accounts_one_global on site_accounts (domain) where profile_id is null;
create trigger site_accounts_touch before update on site_accounts for each row execute function touch_updated_at();
