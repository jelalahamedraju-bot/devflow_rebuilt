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

    # managed 模式下，系统把仓库代码下载到这里（每个仓库一个子文件夹）
    repo_checkout_dir: str = ".data/repos"

    # Webhook 共享密钥：GitHub 用它给每个通知签名，我们用它验证「真的是 GitHub 发来的」
    # 没配时跳过校验——只适合本地开发，部署时必须配置
    github_webhook_secret: str | None = None

    # 定时同步（兜底：Webhook 丢了也能在下一轮补上）
    auto_sync_enabled: bool = True
    auto_sync_interval_seconds: int = 300  # 每 5 分钟一轮；代码里强制至少 30 秒
    auto_sync_limit: int = 5  # 每轮每类数据最多拉几条，控制 GitHub 请求量


settings = Settings()
