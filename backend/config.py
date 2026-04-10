from pydantic_settings import BaseSettings
from pathlib import Path
import os

# Force load .env BEFORE pydantic reads anything
from dotenv import load_dotenv
load_dotenv(
    dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
    override=True
)

class Settings(BaseSettings):
    MONGODB_URL: str = ""
    MONGODB_DB_NAME: str = "salesflow"
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/api/auth/google/callback"
    JWT_SECRET: str = "change-this-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 168
    ANTHROPIC_API_KEY: str = ""
    GROQ_API_KEY: str = ""
    OPENAI_API_KEY: str = ""
    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

   
    FRONTEND_URL: str = "http://localhost:3000"
    ENVIRONMENT: str = "development"
    

    class Config:
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"

settings = Settings()

# Debug — remove after confirming
