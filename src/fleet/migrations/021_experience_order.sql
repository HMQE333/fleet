-- "The latest run on this site" has to be a question with one answer. `created_at` comes from the clock, and a clock
-- has a step: on Windows `datetime.now()` moves in 15.6 ms jumps, so two steps recorded in the same tick share a
-- timestamp to the microsecond and `order by created_at desc` is free to return them in either order - the base then
-- quotes the wrong "last failure" or replays the wrong path. `seq` is the order the rows were written in, the
-- tie-break that makes both answers stable.
alter table experience add column seq bigserial;

create index experience_domain_order_idx on experience (domain, created_at desc, seq desc);
drop index experience_domain_idx;
