from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from dotenv import load_dotenv
from app.api import router

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

