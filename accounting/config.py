from __future__ import annotations

import os
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
    # OAuth リフレッシュ用（自動トークン更新で必須）
    freee_client_id: str = Field(default="", alias="FREEE_CLIENT_ID")
    freee_client_secret: str = Field(default="", alias="FREEE_CLIENT_SECRET")
    # トークン保管場所（atomic write 対象、gitignore 配下に置くこと）
    freee_token_file: str = Field(default="./secrets/freee_tokens.json", alias="FREEE_TOKEN_FILE")
    freee_account_item_inventory: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_INVENTORY")
    freee_account_item_opening_inventory: str = Field(
        default="", alias="FREEE_ACCOUNT_ITEM_OPENING_INVENTORY"
    )
    freee_account_item_sales: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_SALES")
    freee_account_item_receivable: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_RECEIVABLE")
    freee_account_item_commission: str = Field(default="", alias="FREEE_ACCOUNT_ITEM_COMMISSION")
    freee_tax_code_sales: int = Field(default=21, alias="FREEE_TAX_CODE_SALES")
    freee_tax_code_fee: int = Field(default=0, alias="FREEE_TAX_CODE_FEE")

    # Anthropic Claude Vision
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    # Gmail OAuth（vendor-invoice タスクで使用）
    gmail_credentials_file: str = Field(
        default="./secrets/gmail_credentials.json", alias="GMAIL_CREDENTIALS_FILE"
    )
    gmail_token_file: str = Field(
        default="./secrets/gmail_tokens.json", alias="GMAIL_TOKEN_FILE"
    )
    gmail_user_id: str = Field(default="me", alias="GMAIL_USER_ID")
    vendor_invoice_attachments_dir: str = Field(
        default="/tmp/vendor_invoice_attachments",
        alias="VENDOR_INVOICE_ATTACHMENTS_DIR",
    )

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

    # Web UI
    web_host: str = Field(default="0.0.0.0", alias="WEB_HOST")
    web_port: int = Field(default=8080, alias="WEB_PORT")

    @property
    def database_url(self) -> str:
        return f"sqlite:///{Path(self.database_path).resolve()}"

    @property
    def logs_dir(self) -> Path:
        path = Path(__file__).resolve().parent.parent / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def vendor_partner_id(self, slug: str) -> str:
        """env `VENDOR_MAP_{SLUG}` から freee の partner_id を取得する。

        値は `{partner_id},{display_name}` 形式。未設定・プレースホルダなら ValueError。
        戻り値は str（freee API は整数を期待するが、本ハブ内では文字列のまま運ぶ — payload 構築側で int 化する）。
        """
        key = f"VENDOR_MAP_{slug.upper()}"
        raw = os.environ.get(key, "").strip()
        if not raw:
            raise ValueError(f"環境変数 {key} が未設定です（.env に追加してください）")
        partner_id = raw.split(",", 1)[0].strip()
        if not partner_id or partner_id.startswith("__") or partner_id.endswith("__"):
            raise ValueError(
                f"{key} の partner_id がプレースホルダです（{partner_id!r}）。"
                "freee 画面で実IDを確認し、.env を更新してください"
            )
        return partner_id

    def vendor_display_name(self, slug: str) -> str:
        key = f"VENDOR_MAP_{slug.upper()}"
        raw = os.environ.get(key, "").strip()
        if "," not in raw:
            return ""
        return raw.split(",", 1)[1].strip()

    def list_vendors(self) -> dict[str, str]:
        """`VENDOR_MAP_{SLUG}={partner_id},{display_name}` から `{slug: display_name}` を返す。

        プレースホルダ値（`__*__`）の取引先は除外しない（UI 上では選択可能にして、登録時に検証する）。
        """
        result: dict[str, str] = {}
        for key, value in os.environ.items():
            if not key.startswith("VENDOR_MAP_"):
                continue
            slug = key[len("VENDOR_MAP_") :].lower()
            value = value.strip()
            if "," in value:
                _, name = value.split(",", 1)
                result[slug] = name.strip()
            else:
                result[slug] = slug
        return result


settings = Settings()
