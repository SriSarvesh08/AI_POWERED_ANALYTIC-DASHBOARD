"""
Core configuration — loads from environment variables / .env
"""

from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    APP_NAME: str = "DataPilot AI"
    SECRET_KEY: str = "change-me-in-production-use-32-char-random-string"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24  # 24 hours

    # Database (auth + data store)
    DATABASE_URL: str = "postgresql+asyncpg://datapilot:datapilot@localhost:5432/datapilot"
    AUTH_DATABASE_URL: str = "postgresql+asyncpg://datapilot:datapilot@localhost:5432/datapilot_auth"

    # Gemini AI
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"
    GEMINI_EMBEDDING_MODEL: str = "models/gemini-embedding-001"

    # Groq AI (Fallback/Alternative)
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    USE_GROQ: bool = True

    # Apache Superset
    SUPERSET_BASE_URL: str = "http://superset:8088"
    SUPERSET_PUBLIC_URL: str = "http://localhost"
    SUPERSET_ADMIN_USER: str = "admin"
    SUPERSET_ADMIN_PASSWORD: str = "admin"
    SUPERSET_SECRET_KEY: str = "superset-secret"

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",
        "http://localhost:80",
    ]

    # Vector store (pgvector)
    VECTOR_DIMENSION: int = 3072  # gemini-embedding-001 dimension

    # File upload
    UPLOAD_DIR: str = "/tmp/datapilot/uploads"
    MAX_UPLOAD_SIZE_MB: int = 100


settings = Settings()
