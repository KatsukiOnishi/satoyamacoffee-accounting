from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

load_dotenv()


def _bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # freee
    freee_api_key: str = Field(default="", alias="FREEE_API_KEY")
    freee_company_id: str = Field(default="", alias="FREEE_COMPANY_ID")
    freee_account_item_inventory: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_INVENTORY")
    freee_account_item_opening_inventory: str = Field(
        default="", alias="FREEE_ACCOUNT_ITEM_OPENING_INVENTORY"
    )
    freee_account_item_sales: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_SALES")
    freee_account_item_receivable: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_RECEIVABLE")
    freee_account_item_commission: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_COMMISSION")

    # 他システム連携
    coffee_system_base_url: str = Field(default="", alias="COFFEE_SYSTEM_BASE_URL")
    coffee_system_api_key: str = Field(default="", alias="COFFEE_SYSTEM_API_KEY")
    attendance_system_base_url: str = Field(default="", alias="ATTENDANCE_SYSTEM_BASE_URL")
    attendance_system_api_key: str = Field(default="", alias="ATTENDANCE_SYSTEM_API_KEY")
    shopify_shop_domain: str = Field(default="", alias="SHOPIFY_SHOP_DOMAIN")
    shopify_access_token: str = Field(default="", alias="SHOPIFY_ACCESS_TOKEN")

    # メール通知
    resend_api_key: str = Field(default="", alias="RESEND_API_KEY")
    notify_email: str = Field(default="katsuki.onishi@gmail.com", alias="NOTIFY_EMAIL")
    from_email: str = Field(default="noreply@satoyamacoffee.com", alias="FROM_EMAIL")
    notify_on_success: bool = Field(default=False, alias="NOTIFY_ON_SUCCESS")

    # 実行モード
    dry_run: bool = Field(default=True, alias="DRY_RUN")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    database_path: str = Field(default="./accounting.db", alias="DATABASE_PATH")

    @property
    def database_url(self) -> str:
        return f"sqlite:///{Path(self.database_path).resolve()}"

    @property
    def logs_dir(self) -> Path:
        path = Path(__file__).resolve().parent.parent / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
