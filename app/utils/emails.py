import os
import httpx
from typing import Tuple, Optional
from dotenv import load_dotenv

load_dotenv()


def send_visitor_confirmation_email(
    visitor_name: str,
    visitor_email: str,
    property_address: str,
    share_token: Optional[str] = None,
    properties_added: int = 0
) -> Tuple[int, str]:
    """
    Send a confirmation email to a visitor after they complete an open house form

    Args:
        visitor_name: Name of the visitor
        visitor_email: Email address of the visitor
        property_address: Address of the property they visited
        share_token: Optional share token for the personalized collection
        properties_added: Number of properties added to their collection

    Returns:
        Tuple of (status_code, response_text)
    """
    # Get frontend URL from environment or use default
    frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')

    # Build email body (HTML format)
    email_body = f"""<!DOCTYPE html>
<html>
<body>
<p>Dear {visitor_name},</p>

<p>Thank you for visiting the open house at {property_address}!</p>

<p>We appreciate your interest in this property.</p>"""

    # Add collection info if share token is provided
    if share_token:
        share_link = f"{frontend_url}/showcase/{share_token}"
        email_body += f"""
<p>We've created a personalized collection of {properties_added} similar properties based on your preferences.</p>

<p>You can view and interact with your collection here:<br>
<a href="{share_link}">{share_link}</a></p>

<p>Feel free to like, dislike, or favorite properties, and leave comments. We'll be in touch soon to discuss your preferences.</p>"""
    else:
        email_body += "<p>We'll be in touch soon with more information.</p>"

    email_body += """
<p>Best regards,<br>
The Open House Pal</p>
</body>
</html>"""

    # Send the email
    return send_simple_message(
        from_email="noreply@openhousepal.com",
        to_email=visitor_email,
        subject=f"Thank you for visiting {property_address}",
        message=email_body
    )


def send_visitor_new_properties_notification(
    visitor_name: str,
    visitor_email: str,
    collection_name: str,
    new_properties_count: int,
    total_properties: int,
    share_token: str
) -> Tuple[int, str]:
    """
    Send email notification to visitor when new properties are added to their collection

    Args:
        visitor_name: Name of the visitor
        visitor_email: Email address of the visitor
        collection_name: Name of the collection
        new_properties_count: Number of new properties added
        total_properties: Total number of properties in collection
        share_token: Share token for visitor view link

    Returns:
        Tuple of (status_code, response_text)
    """
    # Get frontend URL from environment or use default
    frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
    share_link = f"{frontend_url}/showcase/{share_token}"

    # Build email body (HTML format)
    email_body = f"""<!DOCTYPE html>
<html>
<body>
<p>Dear {visitor_name},</p>

<p>Great news! We've found {new_properties_count} new {'property' if new_properties_count == 1 else 'properties'} that match your preferences for "{collection_name}".</p>

<p><strong>Your Collection Update:</strong></p>
<ul>
<li>New Properties Added: {new_properties_count}</li>
<li>Total Properties in Your Collection: {total_properties}</li>
</ul>

<p>View your updated personalized collection here:<br>
<a href="{share_link}">{share_link}</a></p>

<p>You can like, dislike, favorite properties, and leave comments. We'll be in touch soon to discuss your preferences and answer any questions.</p>

<p>Best regards,<br>
Open House Pal</p>
</body>
</html>"""

    # Send the email
    return send_simple_message(
        from_email="noreply@openhousepal.com",
        to_email=visitor_email,
        subject=f"New properties added to your collection: {collection_name}",
        message=email_body
    )


def send_agent_new_properties_notification(
    agent_name: str,
    agent_email: str,
    collection_name: str,
    visitor_name: str,
    visitor_email: str,
    new_properties_count: int,
    total_properties: int,
    collection_id: str,
    share_token: Optional[str] = None
) -> Tuple[int, str]:
    """
    Send email notification to agent when new properties are added to a collection

    Args:
        agent_name: Name of the agent
        agent_email: Email address of the agent
        collection_name: Name of the collection
        visitor_name: Name of the visitor
        visitor_email: Email of the visitor
        new_properties_count: Number of new properties added
        total_properties: Total number of properties in collection
        collection_id: ID of the collection
        share_token: Share token for visitor view link

    Returns:
        Tuple of (status_code, response_text)
    """
    # Get frontend URL from environment or use default
    frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')

    # Build email body (HTML format)
    email_body = f"""<!DOCTYPE html>
<html>
<body>
<p>Dear {agent_name},</p>

<p>Great news! We've found {new_properties_count} new {'property' if new_properties_count == 1 else 'properties'} matching the preferences for {visitor_name}'s collection "{collection_name}".</p>

<p><strong>Collection Details:</strong></p>
<ul>
<li>Visitor: {visitor_name} ({visitor_email})</li>
<li>New Properties Added: {new_properties_count}</li>
<li>Total Properties in Collection: {total_properties}</li>
</ul>

<p>You can review the collection and these new properties in your dashboard:<br>
<a href="{frontend_url}/showcases">{frontend_url}/showcases</a></p>"""

    # Add visitor share link if available
    if share_token:
        share_link = f"{frontend_url}/showcase/{share_token}"
        email_body += f"""
<p>The visitor can view their personalized collection here:<br>
<a href="{share_link}">{share_link}</a></p>"""

    email_body += """
<p>Best regards,<br>
Open House Pal Automated Property Sync</p>
</body>
</html>"""

    # Send the email
    return send_simple_message(
        from_email="noreply@openhousepal.com",
        to_email=agent_email,
        subject=f"New properties added to {visitor_name}'s collection",
        message=email_body
    )


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
            os.getenv("MAILGUN_URL"),
            auth=("api", os.getenv('MAILGUN_API_KEY')),
            data={
                "from": f"Open House <{from_email if os.getenv('MAILGUN_DEV') == 'no' else os.getenv('MAILGUN_SANDBOX_FROM')}>",
                "to": f"{to_email if os.getenv('MAILGUN_DEV') == 'no' else os.getenv('MAILGUN_SANDBOX_TO')}",
                "subject": subject,
                "html": message
            },
        )
        return response.status_code, response.text
    except Exception as e:
        print(f"Error sending email: {e}")
        return 500, str(e)
