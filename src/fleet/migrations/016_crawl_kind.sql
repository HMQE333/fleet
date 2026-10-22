-- A new worker kind: crawl one section of a public site into a table (url, title, depth, words, text).
-- Its results live in scrape_results, so previews, CSV/JSON, change monitoring and the Updates feed all apply.
alter table subtasks drop constraint subtasks_kind_check;
alter table subtasks add constraint subtasks_kind_check
    check (kind in ('browser', 'browser_fast', 'fetch', 'research', 'video', 'scrape', 'crawl'));
