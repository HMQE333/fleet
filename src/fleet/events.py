"""Audit log. Every event is stored and pushed to the panel via NOTIFY (see 001_init.sql)."""

import logging
from uuid import UUID

from fleet import db, notify, vault

log = logging.getLogger("fleet.events")


async def emit(
    type: str,
    message: str = "",
    *,
    task_id: UUID | None = None,
    subtask_id: UUID | None = None,
    level: str = "info",
    data: dict | None = None,
) -> None:
    # Defense in depth: no API key this process has used may land in the audit log or the panel.
    message = vault.redact(message)
    data = vault.redact_obj(data or {})
    getattr(log, "warning" if level == "warn" else level, log.info)("[%s] %s", type, message)
    p = await db.pool()
    await p.execute(
        "insert into events (task_id, subtask_id, level, type, message, data) values ($1, $2, $3, $4, $5, $6)",
        task_id,
        subtask_id,
        level,
        type,
        message,
        data or {},
    )
    notify.spawn(type, message, task_id, data)  # tells the operator if they set that up (opt-in, never blocks)
