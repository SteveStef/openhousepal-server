"""Cinematic Video renders on AWS Lambda (owner 11/13: "lets use AWS").

The same video the worker (frontend/video-worker/server.mjs) makes, rendered by Remotion Lambda instead:
nothing runs on the OpenHousePal server except these API calls, so the app never slows down while
videos are being made, and many agents' videos are made at once.

Turned on by env (unset = the old self-hosted worker at VIDEO_WORKER_URL):
  REMOTION_FUNCTION_NAME   the deployed render function (frontend/video-worker/lambda/setup.mjs)
  REMOTION_SERVE_URL       the uploaded video template (frontend/video-worker/lambda/deploy-site.mjs -
                           re-run after every change to frontend/motion-video)
  REMOTION_REGION          default us-east-1
  REMOTION_MAX_LAMBDAS     how many Lambdas one video is split across (default 7 - a new AWS account
                           allows 10 at once, and progress checks count too; raise once AWS raises it)
AWS keys: the usual AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (boto3's normal lookup).

Job ids look like "lambda:<bucket>:<renderId>" so a restart picks the render back up (_resume).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

PREFIX = "lambda:"
# measured 11/13 (845 Dale Rd, 900 frames, 9 Lambdas): 445 s -> ~0.49 s a frame at 9 wide. Learned
# from each finished render after that (the Make step's "To make" figure).
_SPEED = {"sec_per_frame": 0.5, "start_seconds": 25.0}
_STARTED: dict[str, float] = {}


def enabled() -> bool:
    return bool(os.environ.get("REMOTION_FUNCTION_NAME") and os.environ.get("REMOTION_SERVE_URL"))


def is_lambda_job(job_id: Optional[str]) -> bool:
    return bool(job_id and job_id.startswith(PREFIX))


def _region() -> str:
    return os.environ.get("REMOTION_REGION") or "us-east-1"


def _client():
    import boto3
    from remotion_lambda import RemotionClient
    return RemotionClient(region=_region(), serve_url=os.environ["REMOTION_SERVE_URL"],
                          function_name=os.environ["REMOTION_FUNCTION_NAME"], session=boto3.Session(region_name=_region()))


def _s3():
    import boto3
    return boto3.client("s3", region_name=_region())


def _split(job_id: str) -> tuple[str, str]:
    _, bucket, render_id = job_id.split(":", 2)
    return bucket, render_id


_ACCOUNT: dict[str, Any] = {"at": 0.0, "limit": None}


def account_limit() -> int:
    """How many Lambdas this AWS account may run at once - read from AWS itself (checked at most once
    an hour), so when AWS raises it (requested 11/13: 10 -> 1,000) videos speed up by themselves with no
    settings change. REMOTION_ACCOUNT_CONCURRENCY overrides; 10 if AWS can't be asked."""
    if os.environ.get("REMOTION_ACCOUNT_CONCURRENCY"):
        return max(2, int(os.environ["REMOTION_ACCOUNT_CONCURRENCY"]))
    if _ACCOUNT["limit"] and time.time() - _ACCOUNT["at"] < 3600:
        return _ACCOUNT["limit"]
    try:
        import boto3
        s = boto3.client("lambda", region_name=_region()).get_account_settings()
        lim = int(((s.get("AccountLimit") or {}).get("ConcurrentExecutions")) or 10)
    except Exception:  # noqa: BLE001
        lim = _ACCOUNT["limit"] or 10
    _ACCOUNT.update(at=time.time(), limit=max(2, lim))
    return _ACCOUNT["limit"]


def lambdas_per_video() -> int:
    """How many Lambdas one video is split across: REMOTION_MAX_LAMBDAS (7 - sized for a 10-at-once
    account) while the account is small; once AWS allows 100+ at once, 40 (about 1-2 minutes a video,
    same cost) unless REMOTION_MAX_LAMBDAS is set higher."""
    env = max(1, int(os.environ.get("REMOTION_MAX_LAMBDAS") or 7))
    return env if account_limit() < 100 else max(env, 40)


def max_videos() -> int:
    """How many videos may be on Lambda at once: each takes lambdas_per_video() renderers + 1 main
    function, and progress checks need a little room, out of the account's limit (10 now -> 1 video at a
    time; the rest wait in line). REMOTION_MAX_VIDEOS overrides."""
    if os.environ.get("REMOTION_MAX_VIDEOS"):
        return max(1, int(os.environ["REMOTION_MAX_VIDEOS"]))
    return max(1, (account_limit() - 2) // (lambdas_per_video() + 1))


def video_seconds(frames: int) -> float:
    """About how long one video takes on Lambda (learned speed), start to finish."""
    return _SPEED["start_seconds"] + max(1, int(frames or 900)) * _SPEED["sec_per_frame"]


def frames_estimate(props: dict) -> int:
    # opening + rooms + end card; the real count comes back from Lambda. A little high on purpose.
    return int(30 * (float(props.get("length") or 30) + 10))


def health() -> dict:
    return {"worker": True, "wait_seconds": 0, "sec_per_frame": round(_SPEED["sec_per_frame"], 3),
            "start_seconds": round(_SPEED["start_seconds"]), "queued": 0, "running": None, "learned": True}


async def start(props: dict) -> str:
    from remotion_lambda import Privacy, RenderMediaParams
    lambdas = lambdas_per_video()
    fpl = max(20, math.ceil(frames_estimate(props) / lambdas))
    params = RenderMediaParams(
        composition="CinematicVideo", input_props=props, codec="h264", crf=21, audio_codec="aac",
        privacy=Privacy.PRIVATE, frames_per_lambda=fpl, concurrency_per_lambda=2, max_retries=1,
        timeout_in_milliseconds=240000,
        # A plain dict in the renderer's own spelling: the library's ChromiumOptions is sent with
        # snake_case keys (disable_web_security), which Remotion ignores - then a photo whose
        # CloudFront copy came back without CORS headers failed to load and its shot was black (QA 11/13).
        chromium_options={"gl": "swangle", "disableWebSecurity": True},  # type: ignore[arg-type]
    )
    res = await asyncio.to_thread(_client().render_media_on_lambda, params)
    if not res:
        raise RuntimeError("worker: AWS didn't start the render")
    job = f"{PREFIX}{res.bucket_name}:{res.render_id}"
    _STARTED[job] = time.time()
    return job


async def status(job_id: str) -> dict:
    """The worker's /jobs/<id> answer, from Lambda's progress. A progress check refused because the
    account's Lambdas are all busy is just 'still going'."""
    bucket, render_id = _split(job_id)
    try:
        p = await asyncio.to_thread(_client().get_render_progress, render_id, bucket, "error")
    except Exception as exc:  # noqa: BLE001 - throttled / network blip: keep waiting
        logger.info("lambda progress check skipped: %s", str(exc)[:200])
        return {"status": "rendering", "progress": None, "busy_check": True}
    if p is None:
        return {"status": "rendering", "progress": None, "busy_check": True}
    meta = getattr(p, "renderMetadata", None)
    meta = meta if isinstance(meta, dict) else {}
    fr = meta.get("frameRange") or []
    dims = meta.get("dimensions") or {}
    frames = (int(fr[1]) - int(fr[0]) + 1) if isinstance(fr, list) and len(fr) == 2 else None
    vc = {"width": dims.get("width"), "fps": 30}  # FPS is fixed (motion-video/types.ts)
    if getattr(p, "fatalErrorEncountered", False):
        errs = getattr(p, "errors", None) or []
        msg = "; ".join(str((e or {}).get("message") if isinstance(e, dict) else e)[:300] for e in errs[:2]) or "render failed"
        return {"status": "failed", "error": msg}
    prog = float(getattr(p, "overallProgress", 0) or 0)
    costs = getattr(p, "costs", None) or {}
    remotion_cost = float((costs.get("accruedSoFar") if isinstance(costs, dict) else 0) or 0)
    out: dict[str, Any] = {"progress": prog, "frames": frames, "width": vc.get("width"), "fps": vc.get("fps") or 30,
                           "remotion_cost_usd": remotion_cost, "lambdas": getattr(p, "lambdasInvoked", None)}
    if getattr(p, "done", False):
        out.update(status="done", progress=1.0, out_key=getattr(p, "outKey", None), out_bucket=bucket)
        t0 = _STARTED.pop(job_id, None)
        if t0 and frames:
            spf = (time.time() - t0) / float(frames)
            _SPEED["sec_per_frame"] = _SPEED["sec_per_frame"] * 0.6 + spf * 0.4
        return out
    out["status"] = "rendering" if prog > 0.02 else "queued"
    t0 = _STARTED.get(job_id)
    if t0 and prog > 0.05:
        el = time.time() - t0
        out["eta_seconds"] = int(el / prog * (1 - prog))
    elif frames:
        out["eta_seconds"] = int(_SPEED["start_seconds"] + frames * _SPEED["sec_per_frame"])
    return out


async def fetch(job_id: str, out_key: Optional[str]) -> bytes:
    bucket, render_id = _split(job_id)
    key = out_key or f"renders/{render_id}/out.mp4"

    def get() -> bytes:
        return _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    return await asyncio.to_thread(get)


async def cleanup(job_id: str) -> None:
    """Delete the render's files from the Remotion bucket once the video is safely in our storage."""
    bucket, render_id = _split(job_id)

    def rm() -> None:
        s3 = _s3()
        tok = None
        while True:
            kw = {"Bucket": bucket, "Prefix": f"renders/{render_id}/"}
            if tok:
                kw["ContinuationToken"] = tok
            r = s3.list_objects_v2(**kw)
            keys = [{"Key": o["Key"]} for o in r.get("Contents") or []]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
            if not r.get("IsTruncated"):
                break
            tok = r.get("NextContinuationToken")
    try:
        await asyncio.to_thread(rm)
    except Exception:  # noqa: BLE001 - leftover files cost a fraction of a cent
        logger.warning("lambda render cleanup failed", exc_info=True)


def charge_usd(remotion_cost: float, config: dict) -> float:
    return round(float(remotion_cost or 0) * float(config.get("studio_motion_lambda_cost_factor", 1.2) or 1.2), 5)


def estimate_usd(seconds: float, config: dict) -> float:
    return round(float(seconds or 30) * float(config.get("studio_motion_render_usd_per_second", 0.0045) or 0.0045), 4)
