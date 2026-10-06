from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/fulfillment"
    secret_key: str = "dev-secret-change-me-0123456789-abcdef"
    access_token_minutes: int = 15
    refresh_token_days: int = 7
    tax_rate: float = 0.10
    bcrypt_rounds: int = 12
    admin_email: str = "admin@example.com"
    admin_password: str = "ChangeMe123!"


settings = Settings()
