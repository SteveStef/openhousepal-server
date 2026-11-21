"""Owner 11/19: "make cinematic also on Amazon". A Cinematic Video's photos (clean copy, depth map, sharpening) go to our
own Amazon graphics machine at Make, like a Fast Cut's - fal retired the depth models the photo service used. When the
machine can't be had, the video still goes ahead (the photo service step after it)."""
import asyncio

import app.main  # noqa: F401
from app.services import gpu_prep_service, studio_motion_service as sms


def _motion():
    return {"kind": "cinematic", "photos": [
        {"id": "a", "source": "https://cdn.example.com/a.jpg"},
        {"id": "b", "source": "https://cdn.example.com/b.jpg"},
        {"id": "c", "source": "https://cdn.example.com/c.jpg", "hidden": True},           # taken out of the video
        {"id": "d", "source": "https://cdn.example.com/d.jpg", "clean": "x", "depth": "y", "sharp": "z"},   # ready already
    ]}


def test_picking_photos_does_not_start_the_photo_service_when_the_machine_is_on(monkeypatch):
    started = []
    monkeypatch.setattr(gpu_prep_service, "enabled", lambda: True)
    monkeypatch.setattr(sms, "_run_prep", lambda asset_id: started.append(asset_id))
    asset = type("A", (), {"id": "cine-1", "options": {"motion": _motion()}})()
    sms.kick_prep(asset)
    assert started == [] and "cine-1" not in sms._PREP


def test_make_sends_every_photo_in_the_video_to_the_machine(monkeypatch):
    sent = []

    async def fake_start(photos, erase):
        sent.extend(p["id"] for p in photos)
        raise RuntimeError("graphics machine: test stop")      # as if Amazon said no - the video must carry on

    async def fake_turn(asset_id, kind, secs, start, busy_exc, saving, after=0.0):
        return await start()

    async def fake_patch(asset_id, fn):
        return None

    monkeypatch.setattr(gpu_prep_service, "start", fake_start)
    monkeypatch.setattr(sms, "_mline_turn", fake_turn)
    monkeypatch.setattr(sms, "_patch", fake_patch)
    motion = _motion()
    out = asyncio.run(sms._gpu_prep("no-such-asset", "owner", motion))
    assert sent == ["a", "b"]          # not the hidden photo, not the one that's ready
    assert out == motion               # nothing lost: the photo service step still gets them
