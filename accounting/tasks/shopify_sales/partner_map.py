"""paymentGatewayNames → freee partner マッピング。

partner_id は仕様書 §11-Q1 の方針通り env から取得する（環境差分・ID変更に強くする）。
"""
from __future__ import annotations

from dataclasses import dataclass

from accounting.tasks.shopify_sales import env as _env


class UnknownGatewayError(Exception):
    """paymentGateway が未対応で安全側で処理を停止する。"""


class PartnerNotConfiguredError(Exception):
    """env に SHOPIFY_SALES_PARTNER_* が設定されていない。"""


@dataclass(frozen=True)
class GatewayResolution:
    slug: str  # env キー suffix
    partner_id: int
    partner_name: str
    canonical_gateway: str  # 例: "shopify_payments" / "KOMOJU"


# slug は env キー（SHOPIFY_SALES_PARTNER_<SLUG>）の suffix。
# canonical_gateway は集計ログ・摘要に出すラベル。
_SHOPIFY_PAYMENTS = "shopify_payments"
_KOMOJU = "komoju"
_AMAZON_PAY = "amazon_pay"
_PAYPAY = "paypay"


def resolve_partner_slug(gateway: str) -> tuple[str, str]:
    """生 paymentGatewayNames[0] → (env slug, canonical_gateway) を返す。

    既知の gateway に該当しないなら UnknownGatewayError。
    """
    if not gateway:
        raise UnknownGatewayError("paymentGateway が空文字です")
    g = gateway.strip()
    if g == "shopify_payments":
        return _SHOPIFY_PAYMENTS, "shopify_payments"
    if g.startswith("KOMOJU"):
        return _KOMOJU, "KOMOJU"
    if g == "Amazon Pay":
        return _AMAZON_PAY, "Amazon Pay"
    if g == "PayPay":
        return _PAYPAY, "PayPay"
    raise UnknownGatewayError(f"未対応の paymentGateway: {gateway!r}")


def resolve(gateway: str) -> GatewayResolution:
    """gateway → GatewayResolution を返す。partner_id は env から動的に取得。

    Raises:
        UnknownGatewayError: 未知の gateway
        PartnerNotConfiguredError: env に partner_id が未設定
    """
    slug, canonical = resolve_partner_slug(gateway)
    partner = _env.shopify_partner(slug)
    if partner is None:
        raise PartnerNotConfiguredError(
            f"env SHOPIFY_SALES_PARTNER_{slug.upper()} が未設定です。"
            f"'<partner_id>,<display_name>' 形式で .env に設定してください "
            f"(対象 gateway: {gateway!r})"
        )
    pid, name = partner
    return GatewayResolution(
        slug=slug,
        partner_id=pid,
        partner_name=name,
        canonical_gateway=canonical,
    )
