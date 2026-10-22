-- Trust rules: the operator's standing OK for ONE exact click ("Send" on example.com, for this profile).
-- Created only from an Inbox approval card, never for payments or sign-ups (see fleet/trust.py).
create table trust_rules (
    id uuid primary key default gen_random_uuid(),
    profile_id uuid not null references profiles(id) on delete cascade,
    domain text not null,
    label text not null,  -- as the operator saw it on the card
    key text not null,    -- the label, normalised, for matching
    uses integer not null default 0,
    last_used_at timestamptz,
    created_at timestamptz not null default now(),
    unique (profile_id, domain, key)
);
