import os
import httpx
import json
from typing import Tuple, Optional, Dict, Any
from dotenv import load_dotenv
from app.config.logging import get_logger
from datetime import datetime

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
        template_variables: Dict[str, Any],
        reply_to: Optional[str] = None
    ) -> Tuple[int, str]:
        # Inject today's date for all templates
        if "today_date" not in template_variables:
            template_variables["today_date"] = datetime.now().strftime("%m/%d/%Y")

        if self.is_dev:
            logger.info(
                "DEV MODE: Email suppressed", 
                extra={
                    "to": to_email, 
                    "subject": subject, 
                    "template": template, 
                    "variables": template_variables,
                    "reply_to": reply_to
                }
            )
            return 200, "Email suppressed in dev mode"

        try:
            client_url = os.getenv('CLIENT_URL', 'https://openhousepal.com')
            api_url = os.getenv('API_URL', 'https://api.openhousepal.com')
            
            # 1. Professional "From" name - e.g., "Sarah from OpenHousePal"
            agent_name = template_variables.get("agent_name")
            from_name = f"{agent_name}" if agent_name else "OpenHousePal"
            
            # 2. Simple text version for better deliverability
            text_body = f"Hello {template_variables.get('recipient_name', 'there')},\n\n"
            text_body += f"You have a new update regarding: {template_variables.get('property_address', 'your showcase')}.\n\n"
            text_body += f"View it here: {template_variables.get('collection_link', template_variables.get('showcase_link', client_url))}\n\n"
            text_body += "Best regards,\nOpenHousePal Team"

            data = {
                "from": f"{from_name} <{self.mailgun_from}>",
                "to": to_email,
                "subject": subject,
                "template": template,
                "text": text_body,
                "t:variables": json.dumps(template_variables),
                "o:tag": template,
                "o:tracking": "no",
                # 3. RFC 8058 One-Click Unsubscribe
                "h:List-Unsubscribe": f"<{api_url}/api/collections/unsubscribe/one-click?email={to_email}>, <{client_url}/unsubscribe?email={to_email}>",
                "h:List-Unsubscribe-Post": "List-Unsubscribe=One-Click"
            }

            if reply_to:
                data["h:Reply-To"] = reply_to

            response = httpx.post(
                self.mailgun_url,
                auth=("api", self.mailgun_api_key),
                data=data,
                timeout=10.0
            )
            return response.status_code, response.text
        except Exception as e:
            logger.error("Error sending email", exc_info=True, extra={"template": template})
            return 500, str(e)
