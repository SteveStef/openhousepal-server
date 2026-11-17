"""Fast Cut photo preparation on a discounted ("spot") Amazon graphics machine (owner 11/16: "do it on Amazon slow
mode to be cheapest"). Save usage only - Make it now keeps the photo service (fal), which needs no machine to start.

The same three jobs the photo service does, with the same open models: the MLS badge painted out (LaMa), the two
depth maps (Depth Anything V2 Small + Marigold v1-1; the app's own _soften_depth / _match_depth finish them exactly
like fal's) and the 2x sharpen (Real-ESRGAN x4plus). Tested 11/16 on 22 photos of 10 Hartfeld Rd on a g5.xlarge:
7 minutes start to finish for about 6 cents, against about 37 cents at the photo service, side by side the same.

One machine per video. It sets itself up, prepares the photos, writes the files through short-lived signed S3 links
(it holds no AWS keys), and switches itself off (which deletes it); a 40-minute dead-man switch makes sure it never
lingers. Graphics machines are scarce and the account may have only one or two at a time, so this is only ever the
FIRST try: anything it did not finish is prepared by the photo service as before - a video is never held up.

On whenever Lambda is on; VIDEO_GPU_PREP=0 turns it off. Job ids look like "gpu:<bucket>:<run>:<instance id>"
so a restart picks the machine's work back up instead of paying for a second one.
Licences: LaMa Apache-2.0, Depth Anything V2 Small Apache-2.0, Real-ESRGAN BSD-3, Marigold weights OpenRAIL++-M.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.services import video_lambda_service as vl

logger = logging.getLogger(__name__)

PREFIX = "gpu:"
_KEYS = "gpu-prep"
_MAX_MINUTES = 40                 # the machine switches itself off by then whatever happens
TYPES = ("g5.xlarge", "g4dn.xlarge", "g6.xlarge", "g4dn.2xlarge")   # first one Amazon has spare (any zone)
_BUCKET: dict = {"name": None}
_PRICE: dict = {}
EXT = {"clean": "jpg", "da": "png", "mg": "png", "sharp": "jpg"}


class GpuBusy(Exception):
    """No graphics machine can be had right now (none spare, or the account's limit)."""


def enabled() -> bool:
    return vl.enabled() and (os.environ.get("VIDEO_GPU_PREP") or "1").strip().lower() not in ("0", "off", "false", "no")


def _region() -> str:
    return os.environ.get("REMOTION_REGION") or "us-east-1"


def _ec2(fast: bool = False):
    import boto3
    from botocore.config import Config
    # one try per zone and machine type: Amazon's own retries made a "none spare" answer take ~10 s each
    return boto3.client("ec2", region_name=_region(), config=Config(retries={"max_attempts": 1, "mode": "standard"}) if fast else None)


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


def price_usd_hr(itype: str, az: Optional[str] = None) -> float:
    """Today's spot price of this machine in the zone it runs in (11/17: the dearest zone was used at first - $0.88
    an hour instead of the $0.53 the machine really cost, so the agent was charged 12.5 cents instead of ~7.5)."""
    key = f"{itype}@{az or '*'}"
    hit = _PRICE.get(key)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        kw = {"AvailabilityZone": az} if az else {}
        r = _ec2().describe_spot_price_history(InstanceTypes=[itype], ProductDescriptions=["Linux/UNIX"],
                                               StartTime=datetime.now(timezone.utc), MaxResults=20, **kw)
        ps = [float(x["SpotPrice"]) for x in r["SpotPriceHistory"]]
        usd = (min(ps) if az else max(ps)) if ps else 0.55
    except Exception:  # noqa: BLE001
        usd = (hit or (0, 0.55))[1]
    _PRICE[key] = (time.time(), usd)
    return usd


# ------------------------------------------------------------------ what runs on the machine

_JOB = r'''
import io, json, sys, time, traceback
import numpy as np
import requests
import torch
from PIL import Image, ImageDraw, ImageOps

job = json.load(open("job.json"))
dev = "cuda"
T = {"gpu": torch.cuda.get_device_name(0), "photos": []}
t0 = time.time()
lama = torch.jit.load("big-lama.pt", map_location=dev).eval()
from transformers import pipeline
da = pipeline("depth-estimation", model="depth-anything/Depth-Anything-V2-Small-hf", device=0)
from diffusers import MarigoldDepthPipeline
mg = MarigoldDepthPipeline.from_pretrained("prs-eth/marigold-depth-v1-1", variant="fp16", torch_dtype=torch.float16).to(dev)
from spandrel import ModelLoader
esr = ModelLoader().load_from_file("RealESRGAN_x4plus.pth").model.to(dev).eval().half()
T["load_s"] = round(time.time() - t0, 1)


def jpg(im, q=95):
    b = io.BytesIO(); im.save(b, "JPEG", quality=q); return b.getvalue()


def png(im):
    b = io.BytesIO(); im.save(b, "PNG"); return b.getvalue()


@torch.no_grad()
def erase(im):
    # the Bright badge sits bottom-left (the app's own mask: 12.5% of the width wide, 4.5% of the width tall)
    w, h = im.size
    cw, ch = min(w, 1024), min(h, 768)
    cw, ch = cw - cw % 8, ch - ch % 8
    box = (0, h - ch, cw, h)
    crop = im.crop(box)
    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rectangle((0, ch - round(w * 0.045), round(w * 0.125), ch), fill=255)
    a = torch.from_numpy(np.asarray(crop).astype(np.float32) / 255).permute(2, 0, 1)[None].to(dev)
    m = torch.from_numpy((np.asarray(mask) > 0).astype(np.float32))[None, None].to(dev)
    out = lama(a, m)[0].permute(1, 2, 0).float().cpu().numpy()
    out = Image.fromarray(np.clip(out * 255, 0, 255).astype(np.uint8))
    res = im.copy(); res.paste(out, box[:2]); return res


@torch.no_grad()
def sharpen(im, tile=512, pad=24):
    w, h = im.size
    x = torch.from_numpy(np.asarray(im).astype(np.float32) / 255).permute(2, 0, 1)[None].to(dev).half()
    out = torch.zeros((3, h * 4, w * 4), dtype=torch.float16, device="cpu")
    for y0 in range(0, h, tile):
        for x0 in range(0, w, tile):
            ya, yb, xa, xb = max(0, y0 - pad), min(h, y0 + tile + pad), max(0, x0 - pad), min(w, x0 + tile + pad)
            o = esr(x[:, :, ya:yb, xa:xb])[0]
            oy, ox = (y0 - ya) * 4, (x0 - xa) * 4
            th, tw = (min(h, y0 + tile) - y0) * 4, (min(w, x0 + tile) - x0) * 4
            out[:, y0 * 4:y0 * 4 + th, x0 * 4:x0 * 4 + tw] = o[:, oy:oy + th, ox:ox + tw].cpu()
    big = Image.fromarray((out.float().clamp(0, 1).permute(1, 2, 0).numpy() * 255).astype(np.uint8))
    tw = min(w * 2, 3200)
    return big.resize((tw, round(h * tw / w)), Image.LANCZOS)


for p in job["photos"]:
    row = {"n": p["n"]}
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(requests.get(p["src"], timeout=60).content))).convert("RGB")
        if im.width > 2048:
            im = im.resize((2048, round(im.height * 2048 / im.width)), Image.LANCZOS)
        clean = erase(im) if job.get("erase") else im
        d1 = da(clean)["depth"]
        d2 = mg(clean, num_inference_steps=4, ensemble_size=1, processing_resolution=1024)
        d2 = mg.image_processor.export_depth_to_16bit_png(d2.prediction)[0]
        sh = sharpen(clean)
        for k, data in (("clean", jpg(clean)), ("da", png(d1)), ("mg", png(d2)), ("sharp", jpg(sh, 92))):
            requests.put(p["put"][k], data=data, timeout=120).raise_for_status()
        row["ok"] = True
    except Exception:
        row["error"] = traceback.format_exc()[-500:]
    T["photos"].append(row)
    requests.put(job["progress"], data=json.dumps({"done": len(T["photos"]), "at": time.time()}).encode(), timeout=30)
T["work_s"] = round(time.time() - t0, 1)
json.dump(T, open("timing.json", "w"))
'''


def _launch(photos: list[dict], erase: bool) -> str:
    import boto3
    from botocore.exceptions import ClientError
    s3 = _s3()
    bucket = _bucket()
    run = uuid.uuid4().hex
    pre = f"{_KEYS}/{run}/"
    get = lambda k: s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": pre + k}, ExpiresIn=3 * 3600)  # noqa: E731
    put = lambda k: s3.generate_presigned_url("put_object", Params={"Bucket": bucket, "Key": pre + k}, ExpiresIn=3 * 3600)  # noqa: E731
    job = {"erase": erase, "progress": put("progress.json"),
           "photos": [{"n": i, "src": p["src"], "put": {k: put(f"{i:03d}_{k}.{e}") for k, e in EXT.items()}} for i, p in enumerate(photos)]}
    s3.put_object(Bucket=bucket, Key=pre + "job.json", Body=json.dumps(job).encode(), ContentType="application/json")
    s3.put_object(Bucket=bucket, Key=pre + "job.py", Body=_JOB.encode(), ContentType="text/plain")
    s3.put_object(Bucket=bucket, Key=pre + "ids.json", Body=json.dumps([p["id"] for p in photos]).encode(), ContentType="application/json")
    user_data = f"""#!/bin/bash
shutdown -h +{_MAX_MINUTES}
exec > /var/log/ohp.log 2>&1
export HOME=/root DEBIAN_FRONTEND=noninteractive
finish() {{
  echo "{{\\"uptime\\": $(cut -d. -f1 /proc/uptime)}}" > /tmp/done.json
  [ -s /opt/j/timing.json ] && curl -s -X PUT -T /opt/j/timing.json '{put("timing.json")}'
  curl -s -X PUT -T /var/log/ohp.log '{put("log.txt")}'
  curl -s -X PUT -T /tmp/done.json '{put("done.json")}'
  shutdown -h now
}}
trap finish EXIT
mkdir -p /opt/j && cd /opt/j
PY=/opt/pytorch/bin/python
[ -x $PY ] || PY=$(which python3)
$PY -m pip install -q transformers diffusers accelerate spandrel requests pillow numpy
$PY -m pip freeze | grep -i -E "^(torch|transformers|diffusers|accelerate|spandrel)=="
curl -sL -o big-lama.pt https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt
curl -sL -o RealESRGAN_x4plus.pth https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth
curl -s '{get("job.json")}' -o job.json
curl -s '{get("job.py")}' -o job.py
$PY job.py
"""
    ssm = boto3.client("ssm", region_name=_region())
    try:
        ami = ssm.get_parameter(Name="/aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-pytorch-2.7-ubuntu-22.04/latest/ami-id")["Parameter"]["Value"]
    except Exception:  # noqa: BLE001 - the same image, found by name
        imgs = _ec2().describe_images(Owners=["amazon"], Filters=[{"Name": "name", "Values": ["Deep Learning OSS Nvidia Driver AMI GPU PyTorch 2.*(Ubuntu 22.04)*"]},
                                                                {"Name": "state", "Values": ["available"]}])["Images"]
        ami = sorted(imgs, key=lambda i: i["CreationDate"])[-1]["ImageId"]
    ec2 = _ec2(fast=True)
    subnets = sorted((x["AvailabilityZone"], x["SubnetId"]) for x in ec2.describe_subnets(Filters=[{"Name": "default-for-az", "Values": ["true"]}])["Subnets"])
    last = ""
    for itype in TYPES:
        for az, sn in subnets:
            try:
                r = ec2.run_instances(
                    ImageId=ami, InstanceType=itype, MinCount=1, MaxCount=1, UserData=user_data, SubnetId=sn,
                    InstanceInitiatedShutdownBehavior="terminate",
                    InstanceMarketOptions={"MarketType": "spot", "SpotOptions": {"SpotInstanceType": "one-time", "InstanceInterruptionBehavior": "terminate"}},
                    TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "ohp-photos-" + run[:10]}, {"Key": "ohp", "Value": "gpu-prep"}]}])
                iid = r["Instances"][0]["InstanceId"]
                s3.put_object(Bucket=bucket, Key=pre + "type.json", Body=json.dumps({"type": itype, "az": az, "at": time.time()}).encode())
                return f"{PREFIX}{bucket}:{run}:{iid}"
            except ClientError as exc:
                last = (exc.response.get("Error") or {}).get("Code") or "error"
                if last.startswith("Unauthorized") or last in ("AuthFailure", "OptInRequired", "InvalidClientTokenId", "Blocked"):
                    # this server's Amazon key may not rent machines: not "busy" - no point waiting for one
                    _cleanup_sync(f"{PREFIX}{bucket}:{run}:none")
                    raise RuntimeError(f"graphics machine not allowed here: {last}") from exc
                if last in ("VcpuLimitExceeded", "MaxSpotInstanceCountExceeded"):
                    break   # the account's limit: no other zone will do either
    _cleanup_sync(f"{PREFIX}{bucket}:{run}:none")
    raise GpuBusy(last)


async def start(photos: list[dict], erase: bool) -> str:
    """photos: [{"id", "src"}]. Rents a machine and sets it preparing them. GpuBusy = none to be had right now."""
    job = await asyncio.to_thread(_launch, photos, erase)
    logger.info("gpu photo machine started", extra={"job": job, "photos": len(photos)})
    return job


def _read(s3, bucket: str, key: str):
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # noqa: BLE001
        if "NoSuchKey" in repr(exc) or "404" in repr(exc):
            return None
        raise


def _status(job_id: str) -> dict:
    """{"state": "working" | "done" | "gone", "done": photos finished, "seconds": machine time so far}"""
    bucket, run, iid = _split(job_id)
    s3 = _s3()
    pre = f"{_KEYS}/{run}/"
    done = _read(s3, bucket, pre + "done.json")
    prog = json.loads(_read(s3, bucket, pre + "progress.json") or b"{}")
    meta = json.loads(_read(s3, bucket, pre + "type.json") or b"{}")
    el = max(0.0, time.time() - float(meta.get("at") or time.time()))
    if done:
        return {"state": "done", "done": int(prog.get("done") or 0), "seconds": el, "type": meta.get("type"), "az": meta.get("az")}
    try:
        inst = _ec2().describe_instances(InstanceIds=[iid])["Reservations"][0]["Instances"][0]
        if inst["State"]["Name"] in ("shutting-down", "terminated", "stopping", "stopped"):
            return {"state": "gone", "done": int(prog.get("done") or 0), "seconds": el, "type": meta.get("type"), "az": meta.get("az")}
    except Exception:  # noqa: BLE001 - a blip: keep waiting
        pass
    if el > _MAX_MINUTES * 60:
        return {"state": "gone", "done": int(prog.get("done") or 0), "seconds": el, "type": meta.get("type")}
    return {"state": "working", "done": int(prog.get("done") or 0), "seconds": el, "type": meta.get("type")}


async def status(job_id: str) -> dict:
    try:
        return await asyncio.to_thread(_status, job_id)
    except Exception as exc:  # noqa: BLE001
        logger.info("gpu photo status check skipped: %s", str(exc)[:200])
        return {"state": "working", "done": 0, "seconds": 0}


def _results(job_id: str) -> dict[str, dict[str, bytes]]:
    """Every photo the machine finished: {photo id: {"clean", "da", "mg", "sharp": bytes}}."""
    bucket, run, _ = _split(job_id)
    s3 = _s3()
    pre = f"{_KEYS}/{run}/"
    ids = json.loads(_read(s3, bucket, pre + "ids.json") or b"[]")
    have = {o["Key"] for o in s3.list_objects_v2(Bucket=bucket, Prefix=pre, MaxKeys=1000).get("Contents") or []}
    out: dict[str, dict[str, bytes]] = {}
    for i, pid in enumerate(ids):
        keys = {k: f"{pre}{i:03d}_{k}.{e}" for k, e in EXT.items()}
        if all(v in have for v in keys.values()):
            out[pid] = {k: s3.get_object(Bucket=bucket, Key=v)["Body"].read() for k, v in keys.items()}
    return out


async def results(job_id: str) -> dict[str, dict[str, bytes]]:
    return await asyncio.to_thread(_results, job_id)


def cost_usd(st: dict) -> float:
    """Machine time (from start until it is gone, about half a minute after it finishes) x today's price."""
    return round((float(st.get("seconds") or 0) + 30.0) / 3600.0 * price_usd_hr(st.get("type") or TYPES[0], st.get("az")), 5)


def _cleanup_sync(job_id: str) -> None:
    bucket, run, iid = _split(job_id)
    if iid != "none":
        try:
            _ec2().terminate_instances(InstanceIds=[iid])
        except Exception:  # noqa: BLE001 - already gone
            pass
    s3 = _s3()
    r = s3.list_objects_v2(Bucket=bucket, Prefix=f"{_KEYS}/{run}/")
    keys = [{"Key": o["Key"]} for o in r.get("Contents") or []]
    if keys:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})


async def cleanup(job_id: str) -> None:
    try:
        await asyncio.to_thread(_cleanup_sync, job_id)
    except Exception:  # noqa: BLE001
        logger.warning("gpu photo cleanup failed", exc_info=True)
