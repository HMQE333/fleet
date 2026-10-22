-- Change monitoring: when a saved automation scrapes the same step again, what changed since the previous run
-- (new / removed / edited items). Null when there was nothing to compare with. See fleet/changes.py.
alter table scrape_results add column changes jsonb;
