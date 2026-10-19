"""CMA accuracy test through the REAL agent UI.

For each home in an accuracy-test batch (created on Admin -> CMA accuracy test, or with --sample
here) this script:
  1. arms the server for (test agent, that sold listing) - cma_backtest_arms - so the wizard sees
     the home as it stood the day before it went under contract;
  2. drives the Studio CMA wizard in a real browser, logged in as the test agent, exactly like an
     agent who accepts the suggestions: Create New CMA -> name + Buyer/Seller -> type the address
     and pick it from the search -> Find Comparables (the wizard widens and auto-picks) -> Continue
     -> Create AI Analysis (full generation, photos included) -> Pricing -> Review -> download the
     PDF -> Share -> open the client page;
  3. scores the saved report against the real sale (same scoreboard as the API runner) and saves
     the review bundle + free text audit + leak checks on the batch item.
A DRY-RUN batch uses zero-cost AI stand-ins on the server (nothing is billed).

    python -m scripts.cma_backtest_ui_runner --batch <id> --email qa-reviewer@example.com --password-file <file> [--shots <dir>]
Run from the backend folder (PYTHONPATH=.). Needs the dev servers on :3000 / API.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import time
from datetime import date, datetime, timezone

from dotenv import load_dotenv

load_dotenv()

import app.main  # noqa: E402,F401  (resolves the app import graph the same way the server does)
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from app.database import AsyncSessionLocal, engine  # noqa: E402
from app.models.database import CmaBacktestArm, CmaBacktestBatch, CmaReport, User  # noqa: E402
from app.services import cma_backtest_arms as arms  # noqa: E402
from app.services.cma_report_audit import audit_report, client_text_sections  # noqa: E402

UI = os.getenv("CMA_TEST_UI_BASE", "http://localhost:3000")


async def _shot(page, shots, name):
    if shots:
        await page.screenshot(path=os.path.join(shots, name), full_page=True)


async def _clear_overlays(page):
    await page.keyboard.press("Escape")
    await page.evaluate("""() => document.querySelectorAll('div.fixed.inset-0').forEach(e => {
        if (getComputedStyle(e).zIndex >= 9000) e.remove() })""")


async def _login(page, email, password):
    await page.goto(f"{UI}/login", wait_until="load", timeout=240000)
    await page.wait_for_selector("input[type=email]", timeout=120000)
    await page.fill("input[type=email]", email)
    await page.fill("input[type=password]", password)
    await page.locator("button[type=submit]").first.click()
    for _ in range(90):
        await asyncio.sleep(1)
        if "/login" not in page.url:
            return
    raise RuntimeError("test agent login failed")


async def run_home(page, item: dict, mode: str, shots: str | None) -> str:
    """Build one CMA through the wizard. Returns the report id."""
    await page.goto(f"{UI}/studio/cma", wait_until="networkidle", timeout=240000)
    await page.wait_for_timeout(2500)
    await _clear_overlays(page)
    await _shot(page, shots, "01_cma_list.png")
    await page.get_by_role("button", name=re.compile("Create New CMA|^Create$")).first.click()
    await page.wait_for_selector("text=New CMA", timeout=60000)
    await page.get_by_placeholder("e.g. The Johnsons").fill("CMA accuracy test")
    await page.get_by_role("button", name="Buyer / Offer" if mode == "buyer" else "Seller / Listing").click()
    await _shot(page, shots, "02_new_cma.png")
    await page.get_by_role("button", name=re.compile("Continue to Subject Property")).click()
    for _ in range(120):
        await asyncio.sleep(1)
        m = re.search(r"/studio/cma/([0-9a-f-]{36})", page.url)
        if m:
            report_id = m.group(1)
            break
    else:
        raise RuntimeError("the new CMA didn't open")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)
    await _clear_overlays(page)

    # Subject: type the address like an agent and pick it from the search results.
    box = page.get_by_placeholder("Start typing an address...")
    await box.click()
    await box.type(f"{item['address']}", delay=60)
    sugg = page.locator("div.absolute.z-20 button", has_text=item["address"].split(" ")[0])
    await sugg.first.wait_for(timeout=60000)
    await _shot(page, shots, "03_address_search.png")
    target = page.locator("div.absolute.z-20 button", has_text=item["address"])
    await (target.first if await target.count() else sugg.first).click()
    await page.get_by_role("button", name=re.compile("Find Comparables")).wait_for(timeout=60000)
    await page.wait_for_timeout(2500)
    await _shot(page, shots, "04_subject.png")
    await page.get_by_role("button", name=re.compile("Find Comparables")).click()

    # Comps: the wizard searches, widens and auto-picks by itself - wait until it settles.
    cont = page.get_by_role("button", name=re.compile(r"^Continue"))
    await cont.first.wait_for(timeout=180000)
    stable, last = 0, None
    for _ in range(240):
        await asyncio.sleep(2)
        txt = await page.inner_text("body")
        settled = "Finding the best comparables" not in txt and "Searching" not in txt
        sig = (settled, txt.count("Added"), len(txt))
        stable = stable + 1 if (settled and sig == last) else 0
        last = sig
        if stable >= 4:
            break
    await _shot(page, shots, "05_comps_autopicked.png")
    await cont.first.click()

    # Analysis: Create AI Analysis (the real generation - photos, notes, valuation, QA).
    gen = page.get_by_role("button", name=re.compile("Create AI Analysis"))
    await gen.wait_for(timeout=120000)
    await page.wait_for_timeout(1500)
    await _shot(page, shots, "06_analysis_before.png")
    await gen.click()
    t0 = time.time()
    while time.time() - t0 < 1800:
        await asyncio.sleep(10)
        async with AsyncSessionLocal() as db:
            rep = await db.get(CmaReport, report_id)
            st = rep.generation_status if rep else None
        if st in ("complete", "failed"):
            break
    await page.reload(wait_until="networkidle")
    await page.wait_for_timeout(4000)
    await _clear_overlays(page)
    await _shot(page, shots, "07_analysis_done.png")
    # Pricing -> Review through the step bar (an agent can move freely between steps).
    for step in ("Pricing", "Review"):
        await page.get_by_role("button", name=step).first.click()
        await page.wait_for_timeout(3000)
        await _shot(page, shots, f"08_{step.lower()}.png")
    return report_id


async def finish_home(page, report_id: str, shots: str | None, out_dir: str) -> dict:
    extra = {}
    dl = page.locator("a", has_text=re.compile("download", re.I)).first
    try:
        await dl.wait_for(timeout=120000)
        async with page.expect_download(timeout=180000) as d:
            await dl.click()
        pdf_path = os.path.join(out_dir, f"{report_id}.pdf")
        await (await d.value).save_as(pdf_path)
        extra["pdf"] = pdf_path
    except Exception as exc:  # noqa: BLE001
        extra["pdf_error"] = str(exc)[:200]
    try:
        await page.get_by_role("button", name=re.compile("^Share$")).first.click()
        await page.wait_for_timeout(3000)
        async with AsyncSessionLocal() as db:
            slug = (await db.get(CmaReport, report_id)).share_slug
        if slug:
            pub = await page.context.new_page()
            await pub.goto(f"{UI}/cma/{slug}", wait_until="networkidle", timeout=240000)
            await pub.wait_for_timeout(5000)
            if shots:
                await pub.screenshot(path=os.path.join(shots, "09_client_page.png"), full_page=True)
            extra["client_page"] = f"/cma/{slug}"
            await pub.close()
    except Exception as exc:  # noqa: BLE001
        extra["share_error"] = str(exc)[:200]
    return extra


async def score(report_id: str, item: dict, arm_id: str) -> dict:
    async with AsyncSessionLocal() as db:
        rep = await db.get(CmaReport, report_id)
        arm = await db.get(CmaBacktestArm, arm_id)
        rd = {"subject": rep.subject, "comparables": rep.comparables, "pricing": rep.pricing, "improvements": rep.improvements}
        est = (rep.pricing or {}).get("ai_estimate") or {}
        v, cp = est.get("value"), item["close_price"]
        f = arm.facts or {}
        out = {
            "report_id": report_id, "status": "leak" if f.get("leak_log") else ("done" if v else "failed"),
            "error": rep.generation_error, "is_backtest_flag": rep.is_backtest,
            "value": v, "range_min": est.get("range_min"), "range_max": est.get("range_max"), "confidence": est.get("confidence"),
            "error_pct": round(100 * (v - cp) / cp, 1) if v else None,
            "in_range": bool(v and est.get("range_min") <= cp <= est.get("range_max")),
            "list_error_pct": round(100 * (item["list_price"] - cp) / cp, 1),
            "export_allowed": (est.get("qa_result") or {}).get("export_allowed"), "qa_blocking": est.get("qa_blocking") or [],
            "cost_usd": (rep.ai_analysis or {}).get("cost_usd") or 0.0,
            "images_sent": sum(len(x) for x in ((rep.ai_analysis or {}).get("images_sent") or {}).values()),
            "ai_steps": ["comp analysis + photos"] + ((rep.ai_analysis or {}).get("followup_calls") or []) + ["final valuation"],
            "leak_checks": f.get("leak_checks"), "leak_log": f.get("leak_log"), "captured_requests": f.get("captured_requests"),
            "ai_input_excerpt": f.get("ai_input_excerpt"),
            "comps": [{"address": c.get("street_address"), "city": c.get("city"), "sale_price": c.get("sale_price"),
                       "close_date": c.get("close_date"), "match": c.get("match_score"), "distance_mi": c.get("distance_miles")}
                      for c in (rep.comparables or [])],
            "audit_findings": audit_report(rd, as_of=date.fromisoformat(item["as_of"]), subject_dom=item.get("dom_as_of")),
            "review": {"client_text": client_text_sections(rd), "subject_as_of": rep.subject,
                       "comps": rep.comparables, "final": {k: est.get(k) for k in ("value", "range_min", "range_max", "confidence", "offer_strategy_fields")},
                       "pricing_fields": {k: v2 for k, v2 in (rep.pricing or {}).items() if k != "ai_estimate"}},
            "runner": "ui", "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        arm.finished_at = datetime.now(timezone.utc)
        await db.commit()
    return out


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--password-file", required=True)
    ap.add_argument("--shots", default=None, help="folder for step screenshots (first home only)")
    ap.add_argument("--out", default=".", help="folder for the downloaded PDFs")
    a = ap.parse_args()
    password = open(a.password_file, encoding="utf-8").read().strip().splitlines()[-1].split()[-1]
    async with AsyncSessionLocal() as db:
        agent = (await db.execute(select(User).where(User.email == a.email))).scalar_one()
        batch = await db.get(CmaBacktestBatch, a.batch)
        batch.status = "running"
        batch.settings = {**(batch.settings or {}), "runner": "ui", "test_agent": a.email}
        await db.commit()
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 1440, "height": 950}, accept_downloads=True)
        page = await ctx.new_page()
        await _login(page, a.email, password)
        first = True
        while True:
            async with AsyncSessionLocal() as db:
                batch = await db.get(CmaBacktestBatch, a.batch)
                if batch.status != "running":
                    break
                idx = next((i for i, it in enumerate(batch.items) if it.get("status") == "queued"), None)
                if idx is None:
                    batch.status = "done"
                    await db.commit()
                    break
                s = batch.settings or {}
                cap, per, spent = float(s.get("cost_cap_usd") or 20), float(s.get("per_home_estimate_usd") or 0), float(batch.actual_cost_usd or 0)
                if not s.get("dry_run") and spent + per > cap:
                    batch.status, batch.error = "stopped", f"Cost cap: ${spent:.2f} spent + ~${per:.2f} would pass ${cap:.2f}."
                    await db.commit()
                    break
                item = dict(batch.items[idx])
                item["status"] = "running"
                batch.items[idx] = item
                flag_modified(batch, "items")
                await db.commit()
                arm = await arms.create_arm(db, owner_id=agent.id, item=item, batch_id=batch.id, item_index=idx,
                                            dry_run=bool(s.get("dry_run")))
                arm_id, mode = arm.id, s.get("mode") or "buyer"
            shots = None
            if first and a.shots:
                shots = a.shots
                os.makedirs(shots, exist_ok=True)
            result: dict = {}
            try:
                report_id = await run_home(page, item, mode, shots)
                result.update(await finish_home(page, report_id, shots, a.out))
                result.update(await score(report_id, item, arm_id))
            except Exception as exc:  # noqa: BLE001 - one home must not stop the batch
                result.update({"status": "failed", "error": f"UI run: {str(exc)[:300]}"})
                async with AsyncSessionLocal() as db:
                    arm = await db.get(CmaBacktestArm, arm_id)
                    arm.finished_at = datetime.now(timezone.utc)
                    await db.commit()
            first = False
            async with AsyncSessionLocal() as db:
                batch = await db.get(CmaBacktestBatch, a.batch)
                item.update(result)
                batch.items[idx] = item
                flag_modified(batch, "items")
                batch.actual_cost_usd = round(sum(float(i.get("cost_usd") or 0) for i in batch.items), 4)
                await db.commit()
            print(item.get("status"), item.get("address"), item.get("city"), "value", item.get("value"), "sold", item.get("close_price"),
                  "err", item.get("error_pct"), "cost", item.get("cost_usd"), (item.get("error") or "")[:200], flush=True)
        await browser.close()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
