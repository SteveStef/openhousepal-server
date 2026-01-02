from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import os
import time
import uuid
import subprocess
from dotenv import load_dotenv
from app.api import router
from app.database import init_db
from app.utils.create_admin import create_admin_user
from app.utils.seed_dummy_data import seed_dummy_data
from app.config.logging import configure_logging, get_logger, set_request_id, clear_request_id

load_dotenv()

CLIENT_URL = os.getenv("CLIENT_URL", "http://localhost:3000")

# Configure logging
configure_logging()
logger = get_logger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Initialize resources
    await init_db()
    await create_admin_user()
    await seed_dummy_data()

    yield

    # Shutdown: Clean up resources
    logger.info("Shutting down application")


app = FastAPI(title="Open House Pal API", lifespan=lifespan)


@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """Log HTTP requests and responses with unique request IDs."""
    # Generate unique request ID
    request_id = str(uuid.uuid4())
    set_request_id(request_id)

    # Extract user_id from JWT token if present
    user_id = None
    try:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
            from jose import jwt
            from app.auth.dependencies import SECRET_KEY, ALGORITHM
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            user_id = payload.get("sub")
    except Exception:
        # If token decode fails, just continue without user_id
        pass

    # Process request and measure duration
    start_time = time.time()
    try:
        response = await call_next(request)
        duration_ms = (time.time() - start_time) * 1000

        # Determine log level based on status code
        if response.status_code < 400:
            log_level = logger.info
        elif response.status_code < 500:
            log_level = logger.warning
        else:
            log_level = logger.error

        log_level(
            "Request completed",
            extra={
                "method": request.method,
                "path": request.url.path,
                #"query_params": str(request.url.query) if request.url.query else None,
                #"client_host": request.client.host if request.client else None,
                "user_id": user_id,
                "status_code": response.status_code,
                "duration_ms": round(duration_ms, 2),
            }
        )

        return response
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        logger.error(
            "Request failed with exception",
            exc_info=True,
            extra={
                "method": request.method,
                "path": request.url.path,
                "user_id": user_id,
                "duration_ms": round(duration_ms, 2),
                "error": str(e),
            }
        )
        raise
    finally:
        clear_request_id()


app.add_middleware(
    CORSMiddleware,
    allow_origins=[CLIENT_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

# Include API routes
app.include_router(router)

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

