from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """从环境变量 / .env 读取配置。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 先用 SQLite 文件；以后换 PostgreSQL 只需改 .env：
    # DATABASE_URL=postgresql+psycopg://devflow:devflow@127.0.0.1:5433/devflow
    database_url: str = "sqlite:///./devflow.db"

    # 公开仓库低频测试可以不配；私有仓库或想要更高额度时在 .env 里设置 GITHUB_TOKEN
    github_token: str | None = None
    github_api_base_url: str = "https://api.github.com"


settings = Settings()
