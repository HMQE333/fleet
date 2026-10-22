"""Research worker: a web-search model (Perplexity by default) answers with sources."""

from fleet import llm, models, prompts
from fleet.executors import ExecResult, RunContext


async def run(ctx: RunContext) -> ExecResult:
    st = ctx.subtask
    user = prompts.worker_context(st["instructions"], ctx.dependency_results)
    if st["start_url"]:
        user += f"\n\nStart here: {st['start_url']}"
    if ctx.task["allowed_domains"]:
        user += f"\n\nOnly use these domains: {', '.join(ctx.task['allowed_domains'])}"
    deep = st["model_tier"] == "smart"  # only reachable when the task allows deep research
    model = await models.model_for("deep_research" if deep else "research")
    await ctx.log(f"Researching with {model}")
    answer = await llm.research(model=model, system=prompts.RESEARCH_SYSTEM, user=user, usage=ctx.usage,
                                max_tokens=16000 if deep else 4000)
    return ExecResult(result=answer)
