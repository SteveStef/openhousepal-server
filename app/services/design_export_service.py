import sys
import asyncio
import os
from app.config.logging import get_logger

logger = get_logger(__name__)

CLIENT_URL = os.getenv("CLIENT_URL", "http://localhost:3000").rstrip("/")

# Real physical page dimensions, inches - kept in sync with each print format's `aspectRatio` in
# frontend/lib/studio/designFormats.ts (deliberately set to match these exact ratios). Only print
# formats need an entry; PDF export isn't offered for social/digital ones.
PRINT_DIMENSIONS_IN = {
    "just_listed_flyer": (8.5, 11),
    "open_house_flyer": (8.5, 11),
    "property_mailer": (6, 4),
    "postcard": (6, 4),
    "postcard_jumbo": (9, 6),
    "letter_mailer": (8.5, 11),
    "door_hanger": (4.25, 11),
}

# Exact publishing size, pixels, for PNG export - mirrors DESIGN_FORMAT_META[...].exportPx in
# frontend/lib/studio/designFormats.ts. Social sizes are what each platform displays natively;
# print formats are their physical size at 300 DPI.
PNG_DIMENSIONS_PX = {
    "instagram_post": (1080, 1080),
    "instagram_portrait": (1080, 1350),
    "just_sold_post": (1080, 1080),
    "instagram_story": (1080, 1920),
    "facebook_post": (1200, 628),
    "linkedin_post": (1200, 628),
    "email_graphic": (1800, 600),
    "presentation_graphic": (1920, 1080),
    "just_listed_flyer": (2550, 3300),
    "open_house_flyer": (2550, 3300),
    "postcard": (1800, 1200),
    "postcard_jumbo": (2700, 1800),
    "letter_mailer": (2550, 3300),
    "property_mailer": (1800, 1200),
    "door_hanger": (1275, 3300),
}

# Viewport pixel density used for the headless render - high enough that type, QR codes, and
# logos come out sharp at real print resolution, not a screenshot's worth of quality.
RENDER_DPI = 300


async def _run_render(render):
    """Runs a headless-browser render coroutine factory. Playwright launches Chromium as a
    subprocess, which Windows' SelectorEventLoop (what uvicorn uses there, e.g. with --reload)
    can't do - it raises NotImplementedError. On that one setup the render runs on its own thread
    with a ProactorEventLoop; everywhere else (Linux/Docker in production) it runs in-line."""
    loop = asyncio.get_running_loop()
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    if sys.platform != "win32" or (proactor and isinstance(loop, proactor)):
        return await render()

    def runner():
        own = asyncio.ProactorEventLoop()
        try:
            return own.run_until_complete(render())
        finally:
            own.close()
    return await asyncio.to_thread(runner)


async def _wait_until_painted(page) -> None:
    """The /print route flips data-design-ready="1" once web fonts are loaded and every photo/logo
    has decoded. networkidle alone can fire before a late image decode or font swap paints."""
    # The ready flag is the real signal (fonts + every photo decoded) - "networkidle" could hang on
    # a slow or long-lived request and failed exports that were otherwise fine.
    try:
        await page.wait_for_selector('[data-design-ready="1"]', timeout=45000)
    except Exception:
        # Older frontend builds don't set the flag - fall back to a short settle.
        logger.warning("Design print route never reported ready; exporting after a fixed settle")
        await page.wait_for_timeout(1200)
    await page.wait_for_timeout(250)


async def render_design_pdf(design_type: str, export_token: str, design_id: str) -> bytes:
    """Launches a headless browser against this app's own /studio/design/{id}/print route (the
    real DesignCanvas component, so the export can never drift from what the editor shows) and
    renders it to a true, physically-sized PDF. Raises RuntimeError with a clear message on any
    failure - the caller turns that into a 502, since this always means the render genuinely
    failed, not a 4xx-shaped client error."""
    from playwright.async_api import async_playwright

    if design_type not in PRINT_DIMENSIONS_IN:
        raise ValueError(f"'{design_type}' is not a print format - PDF export isn't offered for it.")
    width_in, height_in = PRINT_DIMENSIONS_IN[design_type]
    viewport_width = round(width_in * RENDER_DPI)
    viewport_height = round(height_in * RENDER_DPI)
    print_url = f"{CLIENT_URL}/studio/design/{design_id}/print?token={export_token}"

    async def render():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(
                    viewport={"width": viewport_width, "height": viewport_height},
                    device_scale_factor=1,
                )
                await page.goto(print_url, wait_until="domcontentloaded", timeout=60000)
                await _wait_until_painted(page)
                return await page.pdf(
                    width=f"{width_in}in",
                    height=f"{height_in}in",
                    print_background=True,
                    margin={"top": "0in", "bottom": "0in", "left": "0in", "right": "0in"},
                )
            finally:
                await browser.close()

    try:
        return await _run_render(render)
    except Exception as e:
        logger.error("Design PDF export failed", exc_info=True, extra={"design_id": design_id, "design_type": design_type})
        raise RuntimeError(f"PDF export failed: {e}") from e


async def render_design_png(design_type: str, export_token: str, design_id: str, url_override: str | None = None) -> bytes:
    """Same headless render as the PDF path, captured as a PNG at the format's exact publishing
    size (e.g. 1080x1080 for an Instagram post, 300 DPI for print). The canvas is laid out at a
    CSS size that keeps the design's proportions, then rendered with a device scale factor so type,
    QR codes and hairlines stay crisp. `url_override` exists only for local verification."""
    from playwright.async_api import async_playwright

    if design_type not in PNG_DIMENSIONS_PX:
        raise ValueError(f"'{design_type}' has no PNG export size.")
    out_w, out_h = PNG_DIMENSIONS_PX[design_type]
    # Social/digital PNGs render at 2x their platform size (e.g. 2160x2160 for an Instagram post):
    # the platform downsamples it once, which comes out sharper than uploading exactly 1080 px.
    # Print is already 300 DPI.
    if design_type not in PRINT_DIMENSIONS_IN:
        out_w, out_h = out_w * 2, out_h * 2
    # Lay the page out around 1000-1200 CSS px wide (a natural size for the canvas) and let the
    # device scale factor supply the resolution.
    scale = max(1.0, out_w / 1200)
    css_w = round(out_w / scale)
    css_h = round(out_h / scale)
    print_url = url_override or f"{CLIENT_URL}/studio/design/{design_id}/print?token={export_token}"

    async def render():
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(viewport={"width": css_w, "height": css_h}, device_scale_factor=scale)
                await page.goto(print_url, wait_until="domcontentloaded", timeout=60000)
                await _wait_until_painted(page)
                return await page.screenshot(
                    type="png", full_page=False, animations="disabled",
                    clip={"x": 0, "y": 0, "width": css_w, "height": css_h},
                )
            finally:
                await browser.close()

    try:
        return await _run_render(render)
    except Exception as e:
        logger.error("Design PNG export failed", exc_info=True, extra={"design_id": design_id, "design_type": design_type})
        raise RuntimeError(f"PNG export failed: {e}") from e
