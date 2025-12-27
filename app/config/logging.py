"""
Centralized logging configuration for the FastAPI application.

This module provides:
- JSON structured logging for Fluentbit/log aggregation
- Console-only output (stdout/stderr)
- Sensitive data filtering (emails, API keys, passwords)
- Request ID context support via contextvars
- Environment-based log levels
"""

import logging
import os
import re
from contextvars import ContextVar
from typing import Any, Dict, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

from pythonjsonlogger import jsonlogger


# Context variable for request ID tracking across async operations
request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


class SensitiveDataFilter(logging.Filter):

    # Patterns to redact
    EMAIL_PATTERN = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b')
    API_KEY_PATTERN = re.compile(r'(api[_-]?key|apikey|x-api-key|x-rapidapi-key)[\s:=]+[^\s&]+', re.IGNORECASE)
    TOKEN_PATTERN = re.compile(r'(token|bearer|authorization)[\s:=]+[^\s&]+', re.IGNORECASE)
    PASSWORD_PATTERN = re.compile(r'(password|passwd|pwd)[\s:=]+[^\s&]+', re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact sensitive data from log messages."""
        if isinstance(record.msg, str):
            # Redact emails
            record.msg = self.EMAIL_PATTERN.sub('[EMAIL_REDACTED]', record.msg)
            # Redact API keys
            record.msg = self.API_KEY_PATTERN.sub(r'\1=[REDACTED]', record.msg)
            # Redact tokens
            record.msg = self.TOKEN_PATTERN.sub(r'\1=[REDACTED]', record.msg)
            # Redact passwords
            record.msg = self.PASSWORD_PATTERN.sub(r'\1=[REDACTED]', record.msg)

        # Also filter args if present
        if record.args:
            if isinstance(record.args, dict):
                record.args = self._redact_dict(record.args)
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(self._redact_value(arg) for arg in record.args)

        return True

    def _redact_value(self, value: Any) -> Any:
        """Redact sensitive data from a value."""
        if isinstance(value, str):
            value = self.EMAIL_PATTERN.sub('[EMAIL_REDACTED]', value)
            value = self.API_KEY_PATTERN.sub(r'\1=[REDACTED]', value)
            value = self.TOKEN_PATTERN.sub(r'\1=[REDACTED]', value)
            value = self.PASSWORD_PATTERN.sub(r'\1=[REDACTED]', value)
        elif isinstance(value, dict):
            value = self._redact_dict(value)
        elif isinstance(value, (list, tuple)):
            value = type(value)(self._redact_value(item) for item in value)
        return value

    def _redact_dict(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Redact sensitive keys in dictionaries."""
        sensitive_keys = {
            'password', 'passwd', 'pwd', 'secret', 'token',
            'api_key', 'apikey', 'authorization', 'auth'
        }

        redacted = {}
        for key, value in data.items():
            if key.lower() in sensitive_keys:
                redacted[key] = '[REDACTED]'
            else:
                redacted[key] = self._redact_value(value)
        return redacted


class RequestContextFilter(logging.Filter):
    """Add request ID to log records from context."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Add request_id from context to log record."""
        request_id = request_id_var.get()
        record.request_id = request_id if request_id else '-'
        return True


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """Custom JSON formatter with additional fields."""

    def formatTime(self, record, datefmt=None):
        """Override formatTime to convert UTC to Eastern Time (EST/EDT)."""
        # Get the record time as a datetime object
        dt = datetime.fromtimestamp(record.created, tz=ZoneInfo('UTC'))

        # Convert to Eastern Time (automatically handles EST/EDT)
        eastern_dt = dt.astimezone(ZoneInfo('America/New_York'))

        # Format the timestamp
        if datefmt:
            return eastern_dt.strftime(datefmt)
        else:
            return eastern_dt.isoformat()

    def add_fields(self, log_record: Dict[str, Any], record: logging.LogRecord, message_dict: Dict[str, Any]) -> None:
        """Add custom fields to JSON log output."""
        super().add_fields(log_record, record, message_dict)

        # Add timestamp in ISO format (now in EST)
        log_record['timestamp'] = self.formatTime(record, self.datefmt)

        # Add log level
        log_record['level'] = record.levelname

        # Add logger name
        log_record['logger'] = record.name

        # Add request ID if available
        if hasattr(record, 'request_id') and record.request_id != '-':
            log_record['request_id'] = record.request_id

        # Add extra fields if provided
        if hasattr(record, 'extra_fields'):
            log_record.update(record.extra_fields)


def configure_logging(log_level: Optional[str] = None) -> logging.Logger:
    """
    Configure centralized logging for the application.

    Args:
        log_level: Optional log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL)
                  If not provided, reads from LOG_LEVEL env var, defaults to INFO

    Returns:
        Configured root logger

    Environment Variables:
        LOG_LEVEL: Log level (default: INFO)
        LOG_FILE: Path to log file (default: logs/app.log)
        LOG_FILE_ENABLED: Enable file logging (default: true)
        LOG_MAX_BYTES: Max log file size before rotation (default: 10485760 = 10MB)
        LOG_BACKUP_COUNT: Number of backup files to keep (default: 5)
    """
    # Determine log level
    if log_level is None:
        log_level = os.getenv('LOG_LEVEL', 'INFO').upper()

    level = getattr(logging, log_level, logging.INFO)

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # JSON formatter
    formatter = CustomJsonFormatter(
        fmt='%(timestamp)s %(level)s %(name)s %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S%z'
    )

    # Console handler for stdout
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(SensitiveDataFilter())
    console_handler.addFilter(RequestContextFilter())
    root_logger.addHandler(console_handler)

    # File handler with rotation (if enabled)
    log_file_enabled = os.getenv('LOG_FILE_ENABLED', 'true').lower() == 'true'

    if log_file_enabled:
        from logging.handlers import RotatingFileHandler

        log_file = os.getenv('LOG_FILE', 'logs/app.log')
        max_bytes = int(os.getenv('LOG_MAX_BYTES', '10485760'))  # 10MB default
        backup_count = int(os.getenv('LOG_BACKUP_COUNT', '5'))

        # Create logs directory if it doesn't exist
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # Rotating file handler
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(SensitiveDataFilter())
        file_handler.addFilter(RequestContextFilter())
        root_logger.addHandler(file_handler)

    # Suppress overly verbose third-party loggers
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.

    Args:
        name: Logger name (typically __name__ of the module)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


def set_request_id(request_id: str) -> None:
    """
    Set the request ID in the current context.

    Args:
        request_id: Unique identifier for the current request
    """
    request_id_var.set(request_id)


def get_request_id() -> Optional[str]:
    """
    Get the request ID from the current context.

    Returns:
        Request ID if set, None otherwise
    """
    return request_id_var.get()


def clear_request_id() -> None:
    """Clear the request ID from the current context."""
    request_id_var.set(None)
