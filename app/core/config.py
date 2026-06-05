from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Application
    app_name: str = "interview-agent"
    app_env: str = "development"
    secret_key: str  # Required — no default. Server fails at startup if SECRET_KEY missing from .env.
    app_base_url: str = "http://localhost:8000"

    # Database
    database_url: str = "postgresql+asyncpg://postgres:password@localhost:5432/interview_agent"

    # Redis
    redis_url: str = "redis://localhost:6379"

    # AWS
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "ap-south-1"
    s3_bucket_name: str = ""

    # ATS Service
    ats_base_url: str = ""
    ats_service_token: str = ""

    # AI Models
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Voice
    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    livekit_url: str = ""
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # Simli Avatar
    simli_api_key: str = ""
    simli_face_id: str = ""

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "noreply@yourapp.com"

    # Invite token
    invite_token_expire_hours: int = 24  # How long the candidate join link stays valid


settings = Settings()
