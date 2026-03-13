import asyncio
from datetime import datetime
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import AsyncSessionLocal
from app.models.database import ScheduledEmail
from app.services.email_service import EmailService
from app.services.blacklist_service import BlacklistService
from app.config.logging import get_logger

class EmailSchedulerService:
    @staticmethod
    async def process_due_emails():
        """
        Check for pending emails that are due to be sent and process them.
        This function should be called periodically (e.g., every minute).
        """
        async with AsyncSessionLocal() as db:
            try:
                # Find all PENDING emails that are past their scheduled time
                now = datetime.utcnow()
                stmt = select(ScheduledEmail).where(
                    and_(
                        ScheduledEmail.status == "PENDING",
                        ScheduledEmail.scheduled_for <= now
                    )
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
                        status_code, response_text = email_service.send_simple_message(
                            to_email=email_record.recipient_email,
                            subject=email_record.subject,
                            template=email_record.template_name,
                            template_variables=email_record.template_variables
                        )
                        
                        if status_code == 200:
                            email_record.status = "SENT"
                            email_record.sent_at = datetime.utcnow()
                            sent_count += 1
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
                    
                return sent_count
                
            except Exception as e:
                return 0


