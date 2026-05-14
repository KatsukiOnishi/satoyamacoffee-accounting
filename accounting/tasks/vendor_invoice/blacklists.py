"""vendor-invoice タスクの送信者・キーワードリスト。

- EXCLUDED_SENDERS: クレカ自動引落・自社発行・スポット物流系を除外
- EXCLUDED_DOMAINS: 同上をドメイン単位で除外
- KNOWN_BANK_TRANSFER_VENDORS: 銀行振込ベンダー請求書として確定処理する送信者
"""
from __future__ import annotations

from typing import TypedDict


class KnownVendorInfo(TypedDict, total=False):
    partner_name_hint: str
    default_account_item: str
    expects_encrypted_zip: bool


EXCLUDED_SENDERS: set[str] = {
    # クレカ自動引落系
    "billing@shopify.com",
    "feedback@slack.com",
    "invoice+statements@mail.anthropic.com",
    "admin@onamae.com",
    "server@onamae-support.jp",
    "noreply@bsm.freee.work",
    "hello@standartmag.jp",
    # 自社発行
    "noreply@freee.co.jp",
    # 輸入・物流系（スポット、手動運用）
    "ejpcco-gateway@fedex.com",
    "releases@falconcoffees.com",
    "line@falconcoffees.com",
    "georgia@falconcoffees.com",
    "kozue.morinaga@jp.dsv.com",
    "takao.kuwahara@mwt.co.jp",
    # その他広告・通知
    "niimura.misaki@raccoon.ne.jp",
    "info-nkbfarm@nkb.co.jp",
    "renewal@freee.co.jp",
    "noreply@mail.sweeep.ai",
    "shintarou_okuno@sonylife.co.jp",
    "support@cloudsign.jp",
    "merchant-support@paypay-corp.co.jp",
    "info@food-uniform.com",
    "info@uniformnext.com",
    "fromsystem@raccoon.ne.jp",
    "jooto-cs@prtimes.co.jp",
    "toiawase@monotaro.com",
    "member@paid.jp",
    "info@seplumo.com",
}

EXCLUDED_DOMAINS: set[str] = {
    "shopify.com",
    "anthropic.com",
    "slack.com",
    "onamae.com",
    "onamae-support.jp",
    "bsm.freee.work",
    "falconcoffees.com",
    "falconspecialty.com",
    "dsv.com",
    "fedex.com",
    "mwt.co.jp",
    "standartmag.jp",
    "cloudsign.jp",
    "paid.jp",
    "raccoon.ne.jp",
    "nkb.co.jp",
    "sonylife.co.jp",
}

# 送信者 → 銀行振込ベンダー請求書としての確定情報
KNOWN_BANK_TRANSFER_VENDORS: dict[str, KnownVendorInfo] = {
    "management@psi-coffee.com": {
        "partner_name_hint": "株式会社ピーエスアイ",
        "default_account_item": "外注加工費",
    },
    "invoice.myzktax@outlook.jp": {
        "partner_name_hint": "宮﨑会計事務所",
        "default_account_item": "支払手数料",
        "expects_encrypted_zip": True,
    },
}

# 件名・本文に必ず含まれるべき請求書キーワード（OR 判定）
INVOICE_KEYWORDS: tuple[str, ...] = (
    "請求書",
    "ご請求",
    "請求のご案内",
    "お支払い",
    "invoice",
    "Invoice",
    "INVOICE",
)


def is_excluded_sender(sender: str) -> bool:
    """送信者アドレスが除外対象なら True。

    - EXCLUDED_SENDERS の完全一致
    - EXCLUDED_DOMAINS のドメイン一致（user@example.com の example.com）
    """
    if not sender:
        return False
    s = sender.lower().strip()
    if s in EXCLUDED_SENDERS:
        return True
    if "@" in s:
        domain = s.split("@", 1)[1]
        if domain in EXCLUDED_DOMAINS:
            return True
    return False


def get_known_vendor(sender: str) -> KnownVendorInfo | None:
    if not sender:
        return None
    return KNOWN_BANK_TRANSFER_VENDORS.get(sender.lower().strip())


def has_invoice_keyword(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t)
    if not blob:
        return False
    return any(k in blob for k in INVOICE_KEYWORDS)
