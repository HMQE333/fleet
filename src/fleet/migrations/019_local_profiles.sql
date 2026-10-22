-- Local browser profiles: Chrome / Edge profiles Fleet starts itself (own user data directory under data/profiles),
-- no AdsPower needed. `adspower_id` stays the unique handle: local profiles use 'local:<random>'.
alter table profiles add column kind text not null default 'adspower' check (kind in ('adspower', 'local'));
alter table profiles add column headless boolean not null default true;  -- local profiles only: no window unless opened by hand
alter table profiles add column data_dir text;                           -- local profiles only: null = data/profiles/<id>
