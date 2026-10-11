"""Run a controlled AI Media video variant grid for one source photo.

By default this is a dry run so cost projections can be reviewed before spending provider
credits. Add --submit to create the jobs, poll them, and write an HTML comparison report with
preview frames next to each variant.

Examples:
  python scripts/run_studio_video_grid.py --user-email peter@example.com --source-url https://...
  python scripts/run_studio_video_grid.py --submit --user-email peter@example.com --source-url https://...
  python scripts/run_studio_video_grid.py --submit --include-over-budget --user-id USER_ID --source-url https://...
"""
import argparse
import asyncio
import html
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from sqlalchemy import select

from app.database import AsyncSessionLocal, engine
from app.models.database import User
from app.schemas.studio_media import (
    StudioMediaGenerateRequest,
    StudioMediaOptions,
    StudioVideoVariantOptions,
)
from app.services import studio_media_service
from app.services.ai.ai_config_service import ai_config_service


DEFAULT_FOLIAGE_SUFFIX = (
    "Keep foliage, leaves, branches, grass, roof texture, brick, stone, siding and other fine "
    "real-estate textures crisp, sharp, stable and highly detailed throughout the clip. Avoid "
    "soft focus, smeared leaves, mushy trees, motion blur, painterly texture, or temporal detail loss."
)
DEFAULT_MODEL_TIERS = ["fast_pro", "pro"]
DEFAULT_MOTIONS = ["push_in", "slow_push_in", "pan_left"]


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit and review one-photo AI Media video variants.")
    user = parser.add_mutually_exclusive_group(required=True)
    user.add_argument("--user-email", help="OpenHousePal user email that owns the generated assets.")
    user.add_argument("--user-id", help="OpenHousePal user id that owns the generated assets.")
    parser.add_argument("--source-url", required=True, help="Source listing/upload photo URL.")
    parser.add_argument("--source-kind", choices=["listing", "upload"], default="listing")
    parser.add_argument("--model-tier", action="append", choices=["fast_pro", "pro"],
                        help="Repeatable. Defaults to fast_pro and pro.")
    parser.add_argument("--motion", action="append", choices=["push_in", "slow_push_in", "pan_left", "pan_right"],
                        help="Repeatable. Defaults to push_in, slow_push_in, and pan_left.")
    parser.add_argument("--prompt-suffix", action="append",
                        help="Repeatable. Defaults to baseline plus a foliage-detail suffix.")
    parser.add_argument("--max-cost-per-clip-usd", type=float, default=0.65)
    parser.add_argument("--include-over-budget", action="store_true",
                        help="Submit variants even when projected cost exceeds --max-cost-per-clip-usd.")
    parser.add_argument("--submit", action="store_true",
                        help="Actually submit provider jobs. Without this flag, only projections are written.")
    parser.add_argument("--no-poll", action="store_true",
                        help="Submit jobs and exit without waiting for videos/previews.")
    parser.add_argument("--poll-interval-s", type=float, default=10.0)
    parser.add_argument("--timeout-min", type=float, default=45.0)
    parser.add_argument("--out-dir", default="runtime-logs/studio-video-grid")
    return parser.parse_args()


async def _load_user(db, args: argparse.Namespace) -> User:
    if args.user_email:
        res = await db.execute(select(User).where(User.email == args.user_email))
    else:
        res = await db.execute(select(User).where(User.id == args.user_id))
    user = res.scalar_one_or_none()
    if not user:
        raise SystemExit("User not found.")
    return user


def _asset_snapshot(asset) -> dict[str, Any]:
    opts = asset.options or {}
    return {
        "id": asset.id,
        "status": asset.status,
        "provider": asset.provider,
        "provider_model": asset.provider_model,
        "result_url": asset.result_url,
        "preview_frame_url": opts.get("preview_frame_url"),
        "preview_frame_second": opts.get("preview_frame_second"),
        "cost_usd": asset.cost_usd,
        "error_message": asset.error_message,
        "options": opts,
    }


def _write_outputs(out_dir: Path, group_id: str, records: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / f"{group_id}.json"
    report = out_dir / f"{group_id}.html"
    manifest.write_text(json.dumps({"group_id": group_id, "variants": records}, indent=2), encoding="utf-8")
    rows = []
    for record in records:
        asset = record.get("asset") or {}
        projection = record.get("projection") or {}
        preview_url = asset.get("preview_frame_url")
        video_url = asset.get("result_url")
        warning = "OVER" if record.get("projected_over_budget") else ""
        skipped = "SKIPPED" if record.get("skipped") else ""
        actual_cost = asset.get("cost_usd")
        actual_cost_cell = (
            f"<td>${actual_cost:.4f}</td>" if isinstance(actual_cost, (int, float)) else "<td></td>"
        )
        preview = (
            f'<a href="{html.escape(preview_url)}"><img src="{html.escape(preview_url)}" alt="" /></a>'
            if preview_url else ""
        )
        video = f'<a href="{html.escape(video_url)}">video</a>' if video_url else ""
        prompt = "foliage suffix" if record.get("prompt_suffix") else "baseline"
        rows.append(
            "<tr>"
            f"<td>{html.escape(record['label'])}</td>"
            f"<td>{html.escape(record['model_tier'])}</td>"
            f"<td>{html.escape(record['motion'])}</td>"
            f"<td>{html.escape(prompt)}</td>"
            f"<td>${projection.get('per_clip_usd', record.get('projected_cost_usd', 0)):.4f}</td>"
            f"{actual_cost_cell}"
            f"<td>{html.escape(asset.get('status') or record.get('status') or '')} {warning} {skipped}</td>"
            f"<td>{video}</td>"
            f"<td>{preview}</td>"
            "</tr>"
        )
    report.write_text(
        "<!doctype html><html><head><meta charset=\"utf-8\"><title>Studio Video Grid</title>"
        "<style>body{font-family:Arial,sans-serif;margin:24px;color:#17202a}"
        "table{border-collapse:collapse;width:100%}th,td{border:1px solid #d8dee4;padding:8px;vertical-align:top}"
        "th{background:#f6f8fa;text-align:left}img{width:260px;max-width:100%;display:block}"
        "td:nth-child(5),td:nth-child(6){white-space:nowrap}</style></head><body>"
        f"<h1>Studio Video Grid {html.escape(group_id)}</h1>"
        "<table><thead><tr><th>Variant</th><th>Model</th><th>Motion</th><th>Prompt</th>"
        "<th>Projected</th><th>Actual</th><th>Status</th><th>Video</th><th>Preview</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table></body></html>",
        encoding="utf-8",
    )
    print(f"Wrote manifest: {manifest}")
    print(f"Wrote report:   {report}")


async def _main() -> None:
    args = _args()
    model_tiers = args.model_tier or DEFAULT_MODEL_TIERS
    motions = args.motion or DEFAULT_MOTIONS
    prompt_suffixes = args.prompt_suffix if args.prompt_suffix is not None else ["", DEFAULT_FOLIAGE_SUFFIX]
    group_id = str(uuid.uuid4())
    out_dir = Path(args.out_dir)
    records: list[dict[str, Any]] = []

    try:
        async with AsyncSessionLocal() as db:
            user = await _load_user(db, args)
            config = await ai_config_service.get_config(db)

            for model_tier in model_tiers:
                for motion in motions:
                    for prompt_idx, suffix_value in enumerate(prompt_suffixes):
                        suffix = (suffix_value or "").strip()
                        prompt_label = "baseline" if not suffix else f"foliage_prompt_{prompt_idx}"
                        label = f"{model_tier}-{motion}-{prompt_label}"
                        variant = StudioVideoVariantOptions(
                            label=label,
                            model_tier=model_tier,
                            motion=motion,
                            prompt_suffix=suffix or None,
                            max_cost_per_clip_usd=args.max_cost_per_clip_usd,
                        )
                        projection = studio_media_service.estimate_video_generation_cost_usd(
                            config, photo_count=1, variant=variant,
                        )
                        over_budget = projection["per_clip_usd"] > args.max_cost_per_clip_usd + 0.0001
                        record = {
                            "label": label,
                            "model_tier": model_tier,
                            "motion": motion,
                            "prompt_suffix": suffix,
                            "projection": projection,
                            "projected_cost_usd": projection["per_clip_usd"],
                            "projected_over_budget": over_budget,
                            "skipped": bool(over_budget and not args.include_over_budget),
                        }
                        if record["skipped"]:
                            record["status"] = "skipped_over_budget"
                        elif args.submit:
                            asset = await studio_media_service.create_asset(
                                db,
                                user,
                                StudioMediaGenerateRequest(
                                    action="animate",
                                    source_photo_url=args.source_url,
                                    source_kind=args.source_kind,
                                    photos=[args.source_url],
                                    options=StudioMediaOptions(video_variant=variant),
                                ),
                                group_id=group_id,
                            )
                            record["asset"] = _asset_snapshot(asset)
                        else:
                            record["status"] = "dry_run"
                        records.append(record)
                        print(f"{label}: projected ${projection['per_clip_usd']:.4f}"
                              f"{' OVER BUDGET' if over_budget else ''}"
                              f"{' skipped' if record['skipped'] else ''}")

            _write_outputs(out_dir, group_id, records)

            if not args.submit or args.no_poll:
                return

            deadline = time.monotonic() + args.timeout_min * 60.0
            while time.monotonic() < deadline:
                active = False
                for record in records:
                    asset_id = (record.get("asset") or {}).get("id")
                    if not asset_id:
                        continue
                    asset = await studio_media_service.get_owned(db, user.id, asset_id)
                    if asset.status == "processing":
                        active = True
                        asset = await studio_media_service.poll_asset(db, user, asset)
                    record["asset"] = _asset_snapshot(asset)
                _write_outputs(out_dir, group_id, records)
                if not active:
                    return
                await asyncio.sleep(max(2.0, args.poll_interval_s))

            print("Timed out waiting for all variants. Re-run with the same group id from the app if needed.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
