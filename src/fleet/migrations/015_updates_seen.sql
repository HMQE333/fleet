-- Updates: a saved automation's scrape that found changes shows in the Inbox until the operator marks it as seen.
alter table scrape_results add column seen boolean not null default false;
