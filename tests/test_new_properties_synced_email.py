import os
import sys
import argparse
from datetime import datetime
from dotenv import load_dotenv

# Add the server directory to the Python path so we can import 'app'
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Load the environment variables
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

def test_send_new_properties_email(to_email, test_type="new"):
    email_service = EmailService()

    agent_email = "stephenstef456@gmail.com"
    
    if test_type == "drop":
        subject = "Price drop: 123 Maple Ave in Radnor is now $2.9M"
        template = "price_drop_alert"
        template_variables = {
            "recipient_name": "John Smith",
            "agent_name": "Sarah Jenkins",
            "agent_email": agent_email,
            "agent_phone": "(610) 555-0123",
            "collection_link": "https://openhousepal.com/showcase/demo-token",
            "property_address": "123 Maple Ave, Radnor, PA 19087",
            "property_image": "https://images.unsplash.com/photo-1564013799919-ab600027ffc6?auto=format&fit=crop&q=80&w=1000",
            "property_price": "$2,950,000",
            "old_price": "$3,100,000",
            "new_price": "$2,950,000",
            "savings": "$150,000",
            "property_beds": 5,
            "property_baths": 4.5,
            "property_sqft": 4200,
            "total_count": 8,
            "today_date": datetime.now().strftime("%m/%d/%Y"),
            "Unsub": f"https://openhousepal.com/unsubscribe?email={to_email}"
        }
    else:
        subject = "$750K listing just came up in Radnor"
        template = "new_properties_synced"
        template_variables = {
            "recipient_name": "John Smith",
            "agent_name": "Sarah Jenkins",
            "agent_email": agent_email,
            "agent_phone": "(610) 555-0123",
            "collection_link": "https://openhousepal.com/showcase/demo-token",
            "property_address": "456 Oak St, Wayne, PA 19087",
            "property_image": "https://images.unsplash.com/photo-1512917774080-9991f1c4c750?auto=format&fit=crop&q=80&w=1000",
            "property_price": "$750,000",
            "property_beds": 3,
            "property_baths": 2,
            "property_sqft": 1850,
            "total_count": 15,
            "today_date": datetime.now().strftime("%m/%d/%Y"),
            "Unsub": f"https://openhousepal.com/unsubscribe?email={to_email}"
        }

    print(f"Sending test {template} email to {to_email}...")
    status_code, response_text = email_service.send_simple_message(
        to_email=to_email,
        subject=subject,
        template=template,
        template_variables=template_variables,
        reply_to=agent_email
    )

    print(f"Status Code: {status_code}")
    print(f"Response: {response_text}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Send a test new_properties_synced or price_drop_alert email.')
    parser.add_argument('email', help='The recipient email address')
    parser.add_argument('--type', choices=['new', 'drop'], default='new', help='The type of email to send (new listing or price drop)')
    args = parser.parse_args()

    test_send_new_properties_email(args.email, args.type)
