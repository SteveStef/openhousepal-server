import os
import sys
from dotenv import load_dotenv

# Add the server directory to the Python path so we can import 'app'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Force load the .prod.env file
dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../.prod.env'))
print(f"Loading environment from {dotenv_path}")
load_dotenv(dotenv_path, override=True)

# Verify environment variables
print(f"MAILGUN_URL: {os.getenv('MAILGUN_URL')}")
print(f"MAILGUN_DEV: {os.getenv('MAILGUN_DEV')}")
print(f"MAILGUN_FROM: {os.getenv('MAILGUN_FROM')}")

from app.services.email_service import EmailService

def test_send_email():
    email_service = EmailService()

    test_email = "stephenstef456@gmail.com"
    subject = "New Properties Found for Your Showcase - Open House Pal"
    template = "new_properties_synced"

    template_variables = {
        "recipient_name": "Steve",
        "agent_name": "John Smith",
        "agent_email": "john.smith@example.com",
        "agent_phone": "(555) 123-4567",
        "collection_link": "https://openhousepal.com/showcase/test-token",
        "property_address": "123 Main St, Philadelphia, PA 19104",
        "property_image": "https://images.unsplash.com/photo-1568605114967-8130f3a36994",
        "property_price": "$450,000",
        "property_beds": 3,
        "property_baths": 2,
        "property_sqft": 1800,
        "total_count": 5,
        "today_date": "03/13/2026",
        'Unsub': "https://openhousepal.com/unsubscribe"
    }

    print(f"Sending test email ({template}) to {test_email}...")
    status_code, response_text = email_service.send_simple_message(
        to_email=test_email,
        subject=subject,
        template=template,
        template_variables=template_variables
    )

    print(f"Status Code: {status_code}")
    print(f"Response: {response_text}")


if __name__ == "__main__":
    test_send_email()
