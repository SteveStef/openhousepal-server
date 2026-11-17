"""Cinematic Video renders on a discounted ("spot") Amazon machine (owner 11/14: "price over speed -
make this the default").

The same video AWS Lambda makes, from the same uploaded video template (REMOTION_SERVE_URL), made by one
rented machine instead: it starts, sets itself up, makes the video, uploads it to the Remotion bucket and
switches itself off (which deletes it). Measured 11/14 on a c7i.4xlarge: a 30-second video about 6 minutes
start to finish for about 2.5 cents, against about 3 minutes and about 10 cents on Lambda.

Spare machines are not guaranteed (none free, Amazon takes one back, the account's limit is reached), so
this is only ever the FIRST try: studio_motion_service falls back to Lambda by itself whenever a machine
can't be had or doesn't finish - the agent always gets the video.

On whenever Lambda is on; VIDEO_SPOT_RENDER=0 turns it off. VIDEO_SPOT_TYPE picks the machine
(default c7i.4xlarge). Nothing is stored on the machine and it needs no AWS keys: it reads the settings
and writes its files through short-lived signed S3 links.

Job ids look like "spot:<bucket>:<run>:<instance id>" so a restart picks the video back up.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from app.services import video_lambda_service as vl

logger = logging.getLogger(__name__)

PREFIX = "spot:"
REMOTION_VERSION = "4.0.529"   # keep in step with frontend/package.json and the Lambda function
_KEYS = "spot-renders"
_MAX_SECONDS = 40 * 60          # a machine that hasn't finished by then is given up on (and switched off)
_STALE_SECONDS = 240            # no sign of life for this long once it has started = lost
# Learned from each finished video (the Make step's "To make" figure and the countdown).
_SPEED = {"sec_per_frame": 0.34, "start_seconds": 80.0}
_PRICE: dict[str, Any] = {"at": 0.0, "usd_hr": None}
_BUCKET: dict[str, Any] = {"name": None}
_STATE: dict[str, tuple[float, str]] = {}


class SpotBusy(Exception):
    """No machine can be had right now (the account's limit, or Amazon has none spare)."""


def enabled() -> bool:
    return vl.enabled() and (os.environ.get("VIDEO_SPOT_RENDER") or "1").strip().lower() not in ("0", "off", "false", "no")


def is_spot_job(job_id: Optional[str]) -> bool:
    return bool(job_id and job_id.startswith(PREFIX))


def machine_type() -> str:
    return os.environ.get("VIDEO_SPOT_TYPE") or "c7i.4xlarge"


def wait_seconds() -> float:
    """How long a video may wait for a machine before Lambda makes it instead."""
    return float(os.environ.get("VIDEO_SPOT_WAIT_SECONDS") or 240)


def _region() -> str:
    return os.environ.get("REMOTION_REGION") or "us-east-1"


def _ec2():
    import boto3
    return boto3.client("ec2", region_name=_region())


def _s3():
    import boto3
    return boto3.client("s3", region_name=_region())


def _bucket() -> str:
    if not _BUCKET["name"]:
        _BUCKET["name"] = os.environ.get("REMOTION_BUCKET") or next(
            b["Name"] for b in _s3().list_buckets()["Buckets"] if b["Name"].startswith("remotionlambda-"))
    return _BUCKET["name"]


def _split(job_id: str) -> tuple[str, str, str]:
    _, bucket, run, iid = job_id.split(":", 3)
    return bucket, run, iid


def price_usd_hr() -> float:
    """Today's price of the machine (asked at most every 30 minutes; the dearest zone, to be safe)."""
    if _PRICE["usd_hr"] and time.time() - _PRICE["at"] < 1800:
        return _PRICE["usd_hr"]
    try:
        r = _ec2().describe_spot_price_history(InstanceTypes=[machine_type()], ProductDescriptions=["Linux/UNIX"],
                                               StartTime=datetime.now(timezone.utc), MaxResults=20)
        ps = [float(x["SpotPrice"]) for x in r["SpotPriceHistory"]]
        usd = max(ps) if ps else 0.26
    except Exception:  # noqa: BLE001
        usd = _PRICE["usd_hr"] or 0.26
    _PRICE.update(at=time.time(), usd_hr=usd)
    return usd


def video_seconds(frames: int) -> float:
    return _SPEED["start_seconds"] + max(1, int(frames or 900)) * _SPEED["sec_per_frame"]


def estimate_usd(seconds: float, config: dict) -> float:
    """About what one video costs here (machine time x today's price x the same safety factor as Lambda)."""
    secs = video_seconds(int(30 * (float(seconds or 30) + 10))) + 45
    usd = secs / 3600.0 * (_PRICE["usd_hr"] or 0.26)
    return round(vl.charge_usd(usd, config), 4)


def equivalent_usd(frames: Optional[int]) -> float:
    """What a video of this many frames costs on a discounted machine (before the safety factor) - the
    figure an agent's usage counts when the video was made on the FREE Lambda allowance instead
    (owner 11/14: a video that cost us nothing must not tick their usage up at the full Lambda price)."""
    return (video_seconds(int(frames or 900)) + 30.0) / 3600.0 * (_PRICE["usd_hr"] or 0.26)


def health() -> dict:
    return {"worker": True, "wait_seconds": 0, "sec_per_frame": round(_SPEED["sec_per_frame"], 3),
            "start_seconds": round(_SPEED["start_seconds"]), "queued": 0, "running": None, "learned": True}


_NODE = r"""
import os from 'node:os'; import fs from 'node:fs'
import { renderMedia, selectComposition, ensureBrowser } from '@remotion/renderer'
const props = JSON.parse(fs.readFileSync('props.json', 'utf8'))
const serveUrl = process.env.SERVE_URL
const say = (o) => fetch(process.env.PROGRESS_URL, { method: 'PUT', body: Buffer.from(JSON.stringify({ ...o, at: Date.now() })) }).catch(() => {})
await say({ stage: 'browser', progress: 0 })
await ensureBrowser()
const chromiumOptions = { gl: 'swangle', disableWebSecurity: true }
const composition = await selectComposition({ serveUrl, id: process.env.COMPOSITION, inputProps: props, chromiumOptions })
const t0 = Date.now()
let last = 0
await renderMedia({ composition, serveUrl, codec: 'h264', crf: 21, audioCodec: 'aac', outputLocation: 'out.mp4', inputProps: props, chromiumOptions,
  concurrency: os.cpus().length, timeoutInMilliseconds: 180000,
  onProgress: ({ progress, renderedFrames }) => { if (Date.now() - last > 8000) { last = Date.now(); say({ stage: 'render', progress, rendered: renderedFrames, frames: composition.durationInFrames }) } } })
fs.writeFileSync('node.json', JSON.stringify({ render_s: (Date.now() - t0) / 1000, frames: composition.durationInFrames, width: composition.width, height: composition.height, fps: composition.fps }))
"""


def _launch(props: dict, composition: str) -> str:
    import boto3
    from botocore.exceptions import ClientError
    s3 = _s3()
    bucket = _bucket()
    run = uuid.uuid4().hex
    pre = f"{_KEYS}/{run}/"
    s3.put_object(Bucket=bucket, Key=pre + "props.json", Body=json.dumps(props).encode(), ContentType="application/json")
    get = lambda k: s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": pre + k}, ExpiresIn=3600)  # noqa: E731
    put = lambda k: s3.generate_presigned_url("put_object", Params={"Bucket": bucket, "Key": pre + k}, ExpiresIn=3600)  # noqa: E731
    user_data = f"""#!/bin/bash
shutdown -h +{_MAX_SECONDS // 60}
exec > /var/log/ohp.log 2>&1
export HOME=/root DEBIAN_FRONTEND=noninteractive
finish() {{
  OK=false; [ -s /opt/r/out.mp4 ] && [ -f /opt/r/uploaded ] && OK=true
  echo "{{\\"ok\\": $OK, \\"uptime\\": $(cut -d. -f1 /proc/uptime), \\"node\\": $(cat /opt/r/node.json 2>/dev/null || echo null)}}" > /tmp/done.json
  curl -s -X PUT -T /var/log/ohp.log '{put("log.txt")}'
  curl -s -X PUT -T /tmp/done.json '{put("done.json")}'
  shutdown -h now
}}
trap finish EXIT
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get -o DPkg::Lock::Timeout=600 install -y nodejs ca-certificates fonts-liberation libnss3 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libpango-1.0-0 libcairo2 libasound2
mkdir -p /opt/r && cd /opt/r
npm init -y > /dev/null
npm i --no-audit --no-fund @remotion/renderer@{REMOTION_VERSION} remotion@{REMOTION_VERSION} react@19 react-dom@19
curl -s '{get("props.json")}' -o props.json
cat > r.mjs <<'EOF'
{_NODE}
EOF
SERVE_URL='{os.environ["REMOTION_SERVE_URL"]}' PROGRESS_URL='{put("progress.json")}' COMPOSITION='{composition}' node r.mjs
[ -s out.mp4 ] && curl -sf -X PUT -T out.mp4 '{put("out.mp4")}' && touch uploaded
"""
    ami = boto3.client("ssm", region_name=_region()).get_parameter(
        Name="/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id")["Parameter"]["Value"]
    try:
        r = _ec2().run_instances(
            ImageId=ami, InstanceType=machine_type(), MinCount=1, MaxCount=1, UserData=user_data,
            InstanceInitiatedShutdownBehavior="terminate",
            InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}},
            BlockDeviceMappings=[{"DeviceName": "/dev/sda1", "Ebs": {"VolumeSize": 16, "VolumeType": "gp3", "DeleteOnTermination": True}}],
            TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "ohp-video-" + run[:10]}, {"Key": "ohp", "Value": "video-spot"}]}])
    except ClientError as exc:
        try:
            s3.delete_object(Bucket=bucket, Key=pre + "props.json")
        except Exception:  # noqa: BLE001
            pass
        code = (exc.response.get("Error") or {}).get("Code") or ""
        if code in ("MaxSpotInstanceCountExceeded", "InsufficientInstanceCapacity", "SpotMaxPriceTooLow", "InstanceLimitExceeded",
                    "VcpuLimitExceeded", "RequestLimitExceeded", "Unsupported"):
            raise SpotBusy(code) from exc
        raise
    return f"{PREFIX}{bucket}:{run}:{r['Instances'][0]['InstanceId']}"


async def start(props: dict, composition: str = "CinematicVideo") -> str:
    """Rent a machine and set it making the video. SpotBusy = none to be had right now (try again or use
    Lambda); any other error = this route isn't usable here (use Lambda)."""
    job = await asyncio.to_thread(_launch, props, composition)
    logger.info("spot video machine started", extra={"job": job, "type": machine_type()})
    return job


def _read(s3, bucket: str, key: str) -> Optional[dict]:
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read() or b"null")
    except Exception as exc:  # noqa: BLE001
        if "NoSuchKey" in repr(exc) or "404" in repr(exc):
            return None
        raise


def _status(job_id: str) -> dict:
    bucket, run, iid = _split(job_id)
    s3 = _s3()
    pre = f"{_KEYS}/{run}/"
    try:
        done = _read(s3, bucket, pre + "done.json")
        prog = None if done else _read(s3, bucket, pre + "progress.json")
    except Exception as exc:  # noqa: BLE001 - a network blip: keep waiting
        logger.info("spot progress check skipped: %s", str(exc)[:200])
        return {"status": "rendering", "progress": None, "busy_check": True}
    # the machine itself (asked at most every 20 s): when it started, and whether it is still there
    cached = _STATE.get(iid)
    if not cached or time.time() - cached[0] > 20 or done:
        try:
            inst = _ec2().describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
            _STATE[iid] = (time.time(), json.dumps({"state": inst["State"]["Name"], "launch": inst["LaunchTime"].timestamp()}))
        except Exception:  # noqa: BLE001
            if not cached:
                _STATE[iid] = (time.time(), json.dumps({"state": "unknown", "launch": time.time()}))
    info = json.loads(_STATE[iid][1])
    el = max(0.0, time.time() - float(info["launch"]))
    if done:
        node = done.get("node") or {}
        if not done.get("ok"):
            return {"status": "failed", "error": "the rented machine couldn't make the video"}
        frames = int(node.get("frames") or 0) or None
        if frames and node.get("render_s"):
            _SPEED["sec_per_frame"] = _SPEED["sec_per_frame"] * 0.6 + (float(node["render_s"]) / frames) * 0.4
            _SPEED["start_seconds"] = _SPEED["start_seconds"] * 0.6 + max(30.0, el - float(node["render_s"]) - 15) * 0.4
        _STATE.pop(iid, None)
        # the machine is paid for from its start until it is gone (about half a minute after it finishes)
        cost = (el + 30.0) / 3600.0 * price_usd_hr()
        return {"status": "done", "progress": 1.0, "frames": frames, "width": node.get("width"), "fps": node.get("fps") or 30,
                "out_key": pre + "out.mp4", "cost_usd": round(cost, 5), "seconds": round(el)}
    if info["state"] in ("shutting-down", "terminated", "stopping", "stopped"):
        return {"status": "failed", "error": "the rented machine was taken back before it finished"}
    if el > _MAX_SECONDS:
        return {"status": "failed", "error": "the rented machine took too long"}
    out: dict[str, Any] = {"status": "queued", "progress": 0.02}
    frames = int((prog or {}).get("frames") or 0) or 900
    left = max(15.0, video_seconds(frames) - el)
    if prog:
        if time.time() - float(prog.get("at") or 0) / 1000.0 > _STALE_SECONDS:
            return {"status": "failed", "error": "the rented machine stopped answering"}
        p = float(prog.get("progress") or 0)
        if p > 0.03:
            # time per frame so far x frames still to make, + uploading and switching off
            started = max(1.0, el - _SPEED["start_seconds"])
            live = started / p * (1.0 - p) + 20.0
            w = min(1.0, max(0.0, (p - 0.08) / 0.4))
            left = (1.0 - w) * left + w * live
        out["status"] = "rendering" if p > 0 else "queued"
        out["frames"] = frames
    elif el > _STALE_SECONDS + 240:
        return {"status": "failed", "error": "the rented machine never started the video"}
    out["eta_seconds"] = int(left)
    out["progress"] = min(0.97, max(0.02, el / (el + left)))
    return out


async def status(job_id: str) -> dict:
    """The video worker's /jobs/<id> answer (the same shape video_lambda_service.status gives)."""
    try:
        return await asyncio.to_thread(_status, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("spot status check skipped: %s", str(exc)[:200])
        return {"status": "rendering", "progress": None, "busy_check": True}


async def fetch(job_id: str) -> bytes:
    bucket, run, _ = _split(job_id)
    return await asyncio.to_thread(lambda: _s3().get_object(Bucket=bucket, Key=f"{_KEYS}/{run}/out.mp4")["Body"].read())


async def cleanup(job_id: str) -> None:
    """Switch the machine off if it is somehow still on, and delete the run's files."""
    bucket, run, iid = _split(job_id)

    def rm() -> None:
        try:
            _ec2().terminate_instances(InstanceIds=[iid])
        except Exception:  # noqa: BLE001 - already gone
            pass
        s3 = _s3()
        r = s3.list_objects_v2(Bucket=bucket, Prefix=f"{_KEYS}/{run}/")
        keys = [{"Key": o["Key"]} for o in r.get("Contents") or []]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
    try:
        await asyncio.to_thread(rm)
    except Exception:  # noqa: BLE001
        logger.warning("spot render cleanup failed", exc_info=True)
