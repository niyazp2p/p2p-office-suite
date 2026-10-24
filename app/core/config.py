from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "P2P Softwares Office Suite"
    API_V1_STR: str = "/api/v1"
    SECRET_KEY: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # PostgreSQL (Neon Database URLs)
    DATABASE_ASYNC_URL: str
    DATABASE_SYNC_URL: str

    # Redis
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379
    REDIS_URL: str = "redis://127.0.0.1:6379/0"

    @property
    def async_database_url(self) -> str:
        return self.DATABASE_ASYNC_URL

    @property
    def sync_database_url(self) -> str:
        return self.DATABASE_SYNC_URL

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()