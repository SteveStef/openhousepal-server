import os
import httpx
from typing import Tuple
from dotenv import load_dotenv

load_dotenv()


def send_simple_message(from_email: str, to_email: str, subject: str, message: str) -> Tuple[int, str]:
    """
    Send an email using Mailgun API

    Args:
        from_email: Sender email address
        to_email: Recipient email address
        subject: Email subject
        message: Email body text

    Returns:
        Tuple of (status_code, response_text)
    """
    try:
        response = httpx.post(
            "https://api.mailgun.net/v3/sandboxe95d0ed112ce4e9194d1e9a2a0127ea4.mailgun.org/messages",
            auth=("api", os.getenv('MAILGUN_API_KEY')),
            data={
                "from": f"EntryPoint <{from_email if os.getenv('MAILGUN_DEV') == 'no' else os.getenv('MAILGUN_SANDBOX_FROM')}>",
                "to": f"{to_email if os.getenv('MAILGUN_DEV') == 'no' else os.getenv('MAILGUN_SANDBOX_TO')}",
                "subject": subject,
                "text": message
            },
            timeout=10.0
        )
        return response.status_code, response.text
    except Exception as e:
        print(f"Error sending email: {e}")
        return 500, str(e)
