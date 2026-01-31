import os
import asyncio
import httpx

#from dotenv import load_dotenv
#load_dotenv()

from app.config.logging import get_logger

logger = get_logger(__name__)


class DiscordNotifier:
    def __init__(self):
        self.discord_webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        if not self.discord_webhook_url:
            logger.warning("DISCORD_WEBHOOK_URL is not set; Discord notifications are disabled")

    async def _send(self, message: str) -> None:
        if not self.discord_webhook_url:
            return

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(self.discord_webhook_url, json={"content": message})

            if r.status_code in (200, 204):
                logger.debug("Discord webhook sent")
                return

            # Non-success response (log, but do not raise)
            logger.warning(
                "Discord webhook returned non-success status",
                extra={
                    "status_code": r.status_code,
                    "response_text": (r.text[:500] if getattr(r, "text", None) else None),
                },
            )
        except httpx.TimeoutException:
            logger.warning("Discord webhook request timed out")
        except httpx.HTTPError as e:
            # Covers network errors, invalid URL, etc.
            logger.warning("Discord webhook HTTP error", exc_info=e)
        except Exception as e:
            # Safety net
            logger.exception("Unexpected error sending Discord webhook", exc_info=e)

    def send(self, message: str) -> None:
        """
        Fire-and-forget. Returns immediately. Never raises to caller.
        Must be called when an asyncio event loop is running (FastAPI is).
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug(
                "send_nowait called with no running event loop; message not scheduled"
            )
            return

        async def runner():
            try:
                await self._send(message)
            except Exception as e:
                # Should be rare since _send handles errors, but keep as a final guard
                logger.exception("Discord notifier background task failed", exc_info=e)

        loop.create_task(runner())


notifier = DiscordNotifier()

