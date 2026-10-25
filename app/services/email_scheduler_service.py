import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.models.database import ScheduledEmail
from app.services.email_service import EmailService
from app.services.blacklist_service import BlacklistService
from app.config.logging import get_logger

logger = get_logger(__name__)

class EmailSchedulerService:
    @staticmethod
    async def process_due_emails():
        """
        Check for pending emails that are due to be sent and process them.
        This function should be called periodically (e.g., every minute).
        """
        async with AsyncSessionLocal() as db:
            try:
                # Find a limited number of PENDING emails to avoid rate limiting
                now = datetime.now(timezone.utc)
                stmt = (
                    select(ScheduledEmail)
                    .where(
                        and_(
                            ScheduledEmail.status == "PENDING",
                            ScheduledEmail.scheduled_for <= now
                        )
                    )
                    .limit(15)  # Process max 15 emails per minute
                )

                result = await db.execute(stmt)
                due_emails = result.scalars().all()

                if not due_emails:
                    return 0

                email_service = EmailService()
                sent_count = 0

                for email_record in due_emails:
                    try:
                        # Check if recipient is blacklisted
                        if await BlacklistService.is_blacklisted(db, email_record.recipient_email):
                            logger.info(f"Skipping blacklisted email {email_record.id} for {email_record.recipient_email}")
                            email_record.status = "CANCELLED"
                            email_record.error_message = "Recipient is blacklisted"
                            await db.commit()
                            continue

                        logger.info(f"Processing scheduled email {email_record.id} for {email_record.recipient_email}")
                        
                        # Extract agent email for Reply-To header if available
                        reply_to = email_record.template_variables.get("agent_email")
                        
                        # A guest email goes from the agent's own mailbox when they connected one -
                        # tracked, and held back when the engagement rules say so (client_email_service).
                        sent = None
                        track_id = None
                        variables = email_record.template_variables or {}
                        if email_record.owner_id:
                            from app.services.agent_mailbox_service import send_guest
                            from app.services.client_email_service import before_send, strip_private
                            gate = await before_send(email_record.owner_id, email_record.recipient_email, email_record.template_name,
                                                     email_record.subject, variables)
                            if gate is None:
                                email_record.status = "CANCELLED"
                                email_record.error_message = "Held back by the email rules (not opening these updates, or the same email already went to them today)"
                                await db.commit()
                                continue
                            track_id, variables = gate
                            sent = await send_guest(email_record.owner_id, email_record.recipient_email, email_record.subject,
                                                    email_record.template_name, strip_private(variables))
                        status_code, response_text = sent or email_service.send_simple_message(
                            to_email=email_record.recipient_email,
                            subject=email_record.subject,
                            template=email_record.template_name,
                            template_variables={**variables, "_log_owner": email_record.owner_id,
                                                "_log_audience": "client" if email_record.owner_id else None},
                            reply_to=reply_to
                        )
                        if track_id:
                            from app.services.client_email_service import after_send
                            await after_send(track_id, status_code == 200, "mailbox" if sent else "openhousepal", code=status_code, detail=response_text)
                        
                        if status_code == 200:
                            email_record.status = "SENT"
                            email_record.sent_at = datetime.now(timezone.utc)
                            sent_count += 1
                        elif status_code == 429 or "limit exceeded" in response_text.lower():
                            # Stop processing this batch if we hit a rate limit
                            logger.warning(f"Rate limit hit at email {email_record.id}. Stopping batch. Response: {response_text}")
                            break
                        else:
                            email_record.status = "FAILED"
                            email_record.error_message = f"Status: {status_code}, Response: {response_text}"
                            logger.error(f"Failed to send scheduled email {email_record.id}: {response_text}")
                            
                    except Exception as e:
                        email_record.status = "FAILED"
                        email_record.error_message = str(e)
                        logger.error(f"Exception sending scheduled email {email_record.id}: {str(e)}")
                    
                    # Commit updates for each email to ensure progress is saved
                    await db.commit()
                    
                    # Small delay between sends to stay under burst limits
                    await asyncio.sleep(2)
                    
                return sent_count
                
            except Exception as e:
                return 0


