import os
import sys
import argparse
from dotenv import load_dotenv

# Add the server directory to the Python path so we can import 'app'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load the environment variables
# If .prod.env exists, it's likely where the real keys are
dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../.prod.env'))
if os.path.exists(dotenv_path):
    print(f"Loading environment from {dotenv_path}")
    load_dotenv(dotenv_path, override=True)
else:
    print("Loading environment from default .env")
    load_dotenv()

# Ensure MAILGUN_DEV is "no" if we want to actually send the email
if os.getenv("MAILGUN_DEV") != "no":
    print("Warning: MAILGUN_DEV is not set to 'no'. Email might be suppressed.")
    print("To actually send, set MAILGUN_DEV=no in your environment or .prod.env")

from app.services.email_service import EmailService

def test_send_verify_code_email(to_email):
    email_service = EmailService()

    subject = "Verify Your Email - Open House Pal"
    template = "verify_code"

    template_variables = {
        "agent_name": "Test User",
        "verify_code": "123456",
        "expiration_minutes": "15"
    }

    print(f"Sending test verify_code email to {to_email}...")
    status_code, response_text = email_service.send_simple_message(
        to_email=to_email,
        subject=subject,
        template=template,
        template_variables=template_variables
    )

    print(f"Status Code: {status_code}")
    print(f"Response: {response_text}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Send a test verify_code email.')
    parser.add_argument('email', help='The recipient email address')
    args = parser.parse_args()

    test_send_verify_code_email(args.email)
