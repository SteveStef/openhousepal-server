import os
import httpx
import json
from typing import Tuple, Optional, Dict, Any
from dotenv import load_dotenv
from app.config.logging import get_logger

load_dotenv()

logger = get_logger(__name__)

class EmailService:
    def __init__(self):
        self.mailgun_url = os.getenv("MAILGUN_URL")
        self.mailgun_api_key = os.getenv('MAILGUN_API_KEY')
        self.mailgun_from = os.getenv("MAILGUN_FROM")
        self.is_dev = os.getenv("MAILGUN_DEV", "yes") == "yes"

    def send_simple_message(
        self,
        to_email: str,
        subject: str,
        template: str,
        template_variables: Dict[str, Any]
    ) -> Tuple[int, str]:
        # Skip sending in dev mode
        if self.is_dev:
            logger.info(
                f"[DEV MODE] Email not sent - would have sent to: {to_email}",
                extra={
                    "subject": subject,
                    "template": template,
                    "to_email": to_email
                }
            )
            return 200, "Dev mode - email not sent"

        try:
            response = httpx.post(
                self.mailgun_url,
                auth=("api", self.mailgun_api_key),
                data={
                    "from": f"OpenHousePal <{self.mailgun_from}>",
                    "to": to_email,
                    "subject": subject,
                    "template": template,
                    "h:X-Mailgun-Variables": json.dumps(template_variables)
                },
                timeout=10.0
            )
            return response.status_code, response.text
        except Exception as e:
            logger.error("Error sending email", exc_info=True, extra={"template": template})
            return 500, str(e)
