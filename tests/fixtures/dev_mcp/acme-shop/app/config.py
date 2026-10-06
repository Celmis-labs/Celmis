"""Settings for the shop service, read from the environment."""

import os


class Settings:
    """Every value comes from an environment variable; nothing is stored here."""

    def __init__(self) -> None:
        self.db_host = os.getenv("DB_HOST", "localhost")
        self.db_port = int(os.getenv("DB_PORT", "5432"))
        self.db_name = os.getenv("DB_NAME", "shop")
        self.db_user = os.getenv("DB_USER", "shop_app")
        self.db_password = os.environ["DB_PASSWORD"]
        self.jwt_issuer = os.getenv("JWT_ISSUER", "https://auth.example.com/realms/acme")
        self.jwt_audience = os.getenv("JWT_AUDIENCE", "shop-api")
        self.jwt_jwks_url = os.environ["JWT_JWKS_URL"]
        self.log_level = os.getenv("LOG_LEVEL", "INFO")


def get_settings() -> Settings:
    return Settings()
