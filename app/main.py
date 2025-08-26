from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from dotenv import load_dotenv
from app.api import router
from app.scheduler import start_scheduler, stop_scheduler

load_dotenv()

app = FastAPI(title="Open House Pal API")

# CORS Configuration
CLIENT_URL = os.getenv("CLIENT_URL", "http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[CLIENT_URL, "http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["*"],
)

# Startup and shutdown events
@app.on_event("startup")
async def startup_event():
    """Start the property sync scheduler on application startup"""
    try:
        await start_scheduler()
    except Exception as e:
        print(f"Warning: Failed to start property sync scheduler: {e}")
        # Continue startup even if scheduler fails

@app.on_event("shutdown")
async def shutdown_event():
    """Stop the property sync scheduler on application shutdown"""
    await stop_scheduler()

# Include API routes
app.include_router(router)

@app.get("/")
async def root():
    return {"message": "Hello World"}

@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)

