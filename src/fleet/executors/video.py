"""Video worker: a video-capable model (Gemini by default) watches a video and answers the instructions."""

from urllib.parse import urlparse

from fleet import llm, models, prompts
from fleet.executors import Blocked, ExecResult, RunContext

YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}


def is_youtube(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in YOUTUBE_HOSTS


async def run(ctx: RunContext) -> ExecResult:
    st = ctx.subtask
    url = st["start_url"]
    if not url:
        raise Blocked("A video subtask needs the video's URL")
    model = await models.model_for("video")
    extra: dict = {}
    if is_youtube(url):
        # OpenRouter only accepts YouTube links for Gemini served by Google AI Studio.
        if not model.startswith("google/"):
            raise Blocked(f"YouTube links need a Google Gemini model; the video model is {model}. Change it in Settings → Models.")
        extra["provider"] = {"only": ["google-ai-studio"]}
    await ctx.log(f"Watching {url} with {model}")
    user = [
        {"type": "text", "text": prompts.worker_context(st["instructions"], ctx.dependency_results)},
        {"type": "video_url", "video_url": {"url": url}},
    ]
    answer = await llm.complete(model=model, system=prompts.VIDEO_SYSTEM, user=user, usage=ctx.usage, **extra)
    return ExecResult(result=answer)
