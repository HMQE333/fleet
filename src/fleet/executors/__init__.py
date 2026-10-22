"""Worker executors. Each takes a RunContext and returns an ExecResult."""

from dataclasses import dataclass, field

from fleet import events, llm


class Blocked(RuntimeError):
    """The site refused us (e.g. bot check on a public fetch). The planner can reroute to a browser."""


@dataclass
class RunContext:
    task: dict
    subtask: dict
    dependency_results: dict[str, str]
    worker_id: str
    usage: llm.Usage = field(default_factory=llm.Usage)
    profile: dict | None = None

    async def log(self, message: str, *, type: str = "log", level: str = "info", data: dict | None = None) -> None:
        await events.emit(
            type, message, task_id=self.task["id"], subtask_id=self.subtask["id"], level=level, data=data
        )


@dataclass
class ExecResult:
    result: str
    success: bool = True
    steps: int = 0
    history_path: str | None = None
    trace: list[dict] | None = None  # browser steps as a replayable path (see fastbrowser/trace.py)
