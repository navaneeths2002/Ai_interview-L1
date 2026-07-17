from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # .env also carries operational toggles consumed directly via os.environ
        # (voice capture/analysis, avatar watchdog, recordings dir, etc.) that are
        # intentionally NOT typed here. Ignore them instead of rejecting startup.
        extra="ignore",
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

    # ── ATS Integration (single-call: ATS sends IDs, we pull from their DB) ──
    # Read-only connection string to the ATS module's MySQL db (a SEPARATE db —
    # our own app is Postgres). Format:
    #   mysql+aiomysql://user:password@host:3306/dbname
    ats_database_url: str = ""
    # The API key WE generate and hand to the ATS team. They send it as the
    # X-API-Key header when calling POST /api/v1/integration/interview.
    connector_api_key: str = ""

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
    avatar_enabled: bool = True  # false → reliable voice-only mode (no avatar single-point-of-failure)
    avatar_fallback_to_room_audio: bool = True  # if avatar can't (re)start, route voice via room audio
    avatar_watchdog_enabled: bool = True  # probe avatar health mid-interview; self-heal (restart → room-audio fallback)

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "noreply@yourapp.com"

    # Invite token
    invite_token_expire_hours: int = 24  # How long the candidate join link stays valid


settings = Settings()
