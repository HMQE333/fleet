-- API keys, encrypted with AES-256-GCM. The master key lives outside the database
-- (FLEET_MASTER_KEY or the OS credential store), so a database dump alone reveals nothing.
create table secrets (
    name         text primary key check (name ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    ciphertext   bytea not null,
    nonce        bytea not null,
    key_id       text not null,   -- fingerprint of the master key that encrypted it
    hint         text not null default '',   -- last 4 characters, for recognising the key
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    last_used_at timestamptz
);

-- Small operator settings, e.g. which OpenRouter model each role uses.
create table settings (
    key        text primary key,
    value      jsonb not null,
    updated_at timestamptz not null default now()
);

-- New worker kind: watch a video and answer questions about it.
alter table subtasks drop constraint subtasks_kind_check;
alter table subtasks add constraint subtasks_kind_check check (kind in ('browser', 'fetch', 'research', 'video'));
