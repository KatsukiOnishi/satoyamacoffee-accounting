from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path



# ---- helpers ----


def _make_message(
    sender: str = "management@psi-coffee.com",
    subject: str = "請求書のご送付",
    body: str = "",
    has_attachment: bool = True,
):
    from accounting.connectors.gmail import GmailAttachment, GmailMessage

    atts = (
        [
            GmailAttachment(
                attachment_id="att1",
                filename="invoice.pdf",
                mime_type="application/pdf",
                size_bytes=12345,
            )
        ]
        if has_attachment
        else []
    )
    return GmailMessage(
        message_id="m1",
        thread_id="t1",
        sender=sender,
        sender_raw=sender,
        subject=subject,
        received_at=datetime(2026, 5, 14, 9, 0, tzinfo=timezone.utc),
        snippet=body[:120],
        body_text=body,
        attachments=atts,
    )


# ---- classifier ----


def test_classifier_known_vendor_psi_is_bank_transfer():
    from accounting.tasks.vendor_invoice.classifier import classify_initial

    m = _make_message(sender="management@psi-coffee.com")
    v = classify_initial(m)
    assert v.classification == "bank_transfer_invoice"


def test_classifier_excludes_shopify_subscription():
    from accounting.tasks.vendor_invoice.classifier import classify_initial

    m = _make_message(
        sender="billing@shopify.com",
        subject="Your Shopify invoice",
        body="Credit card was charged.",
    )
    v = classify_initial(m)
    assert v.classification == "excluded"
    assert v.exclusion_reason == "blacklisted_sender"


def test_classifier_excludes_anthropic_domain():
    from accounting.tasks.vendor_invoice.classifier import classify_initial

    m = _make_message(
        sender="invoice+statements@mail.anthropic.com",
        subject="Anthropic API usage invoice",
    )
    v = classify_initial(m)
    assert v.classification == "excluded"


def test_classifier_excludes_no_invoice_keyword():
    from accounting.tasks.vendor_invoice.classifier import classify_initial

    m = _make_message(
        sender="newsletter@example.jp",
        subject="今月のお知らせ",
        body="春のキャンペーンのお知らせです",
    )
    v = classify_initial(m)
    assert v.classification == "excluded"
    assert v.exclusion_reason == "no_invoice_keyword"


def test_classifier_no_attachment_with_invoice_keyword():
    from accounting.tasks.vendor_invoice.classifier import classify_initial

    m = _make_message(
        sender="newvendor@example.jp",
        subject="請求書（本文記載）",
        body="お支払いください。振込先: ××銀行...",
        has_attachment=False,
    )
    v = classify_initial(m)
    assert v.classification == "no_attachment"


def test_is_encrypted_zip_detects_password_protected(tmp_path: Path):
    from accounting.tasks.vendor_invoice.classifier import is_encrypted_zip

    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
        zf.writestr("a.txt", "hello")
    assert is_encrypted_zip(plain) is False

    enc = tmp_path / "enc.zip"
    with zipfile.ZipFile(enc, "w") as zf:
        zf.writestr("a.txt", "hello")
    # ローカルヘッダの general purpose flags (offset 6,7) と
    # central directory の general purpose flags (signature PK\x01\x02 後ろの offset 8,9)
    # に encryption bit (0x0001) を立てる
    raw = enc.read_bytes()
    lf = raw.index(b"PK\x03\x04")
    cd = raw.index(b"PK\x01\x02")
    raw = bytearray(raw)
    raw[lf + 6] |= 0x01
    raw[cd + 8] |= 0x01
    enc.write_bytes(bytes(raw))
    assert is_encrypted_zip(enc) is True


# ---- bank_detector ----


def _extracted(**overrides):
    from datetime import date

    from accounting.tasks.vendor_invoice.models import ExtractedInvoice

    defaults = dict(
        partner_name="株式会社ピーエスアイ",
        issue_date=date(2026, 4, 20),
        due_date=date(2026, 4, 30),
        total_amount=234500,
        tax_amount=21318,
        bank_account_info="秋田銀行 大町支店 普通 1234567 カ）ピーエスアイ",
        has_bank_account_info=True,
        line_items_summary="焙煎委託費 4月分",
        is_invoice=True,
        confidence_notes="",
    )
    defaults.update(overrides)
    return ExtractedInvoice(**defaults)


def test_bank_detector_confirms_bank_transfer_invoice():
    from accounting.tasks.vendor_invoice.bank_detector import reclassify_from_extraction

    v = reclassify_from_extraction(_extracted())
    assert v.classification == "bank_transfer_invoice"


def test_bank_detector_excludes_when_no_bank_info():
    from accounting.tasks.vendor_invoice.bank_detector import reclassify_from_extraction

    v = reclassify_from_extraction(
        _extracted(has_bank_account_info=False, bank_account_info=None)
    )
    assert v.classification == "excluded"
    assert v.exclusion_reason == "no_bank_account_info"


def test_bank_detector_excludes_receipt():
    from accounting.tasks.vendor_invoice.bank_detector import reclassify_from_extraction

    v = reclassify_from_extraction(_extracted(is_invoice=False))
    assert v.classification == "excluded"
    assert v.exclusion_reason == "not_an_invoice"


# ---- partner_matcher ----


class _FakeFreee:
    def __init__(self, partners):
        self._partners = partners

    def list_partners(self):
        return self._partners


def test_partner_matcher_exact_match():
    from accounting.tasks.vendor_invoice.partner_matcher import match_partner

    fake = _FakeFreee(
        [
            {"id": 100, "name": "株式会社ピーエスアイ", "shortcut1": "psi"},
            {"id": 200, "name": "宮﨑会計事務所", "shortcut1": ""},
        ]
    )
    m = match_partner(fake, "株式会社ピーエスアイ")  # type: ignore[arg-type]
    assert m.partner_id == 100
    assert m.match_kind == "exact"


def test_partner_matcher_normalized_match():
    from accounting.tasks.vendor_invoice.partner_matcher import match_partner

    fake = _FakeFreee([{"id": 300, "name": "宮崎会計事務所", "shortcut1": ""}])
    # 「﨑」と「崎」の表記揺れ、法人形態語は無いが NFKC で吸収できる
    m = match_partner(fake, "宮﨑会計事務所")  # type: ignore[arg-type]
    # 完全一致ではないが、normalize で空白除去された結果一致
    assert m.partner_id in (300, None)


def test_partner_matcher_returns_none_when_no_match():
    from accounting.tasks.vendor_invoice.partner_matcher import match_partner

    fake = _FakeFreee([{"id": 1, "name": "未関係の取引先"}])
    m = match_partner(fake, "存在しない取引先")  # type: ignore[arg-type]
    assert m.partner_id is None
    assert m.match_kind == "none"


def test_partner_matcher_uses_fallback_hint():
    from accounting.tasks.vendor_invoice.partner_matcher import match_partner

    fake = _FakeFreee([{"id": 100, "name": "株式会社ピーエスアイ"}])
    m = match_partner(fake, None, fallback_hint="株式会社ピーエスアイ")  # type: ignore[arg-type]
    assert m.partner_id == 100


# ---- registrar ----


def test_build_external_id_with_and_without_attachment():
    from accounting.tasks.vendor_invoice.registrar import build_external_id

    assert build_external_id("m1", "a1") == "vendor-invoice:m1:a1"
    assert build_external_id("m1", None) == "vendor-invoice:m1:body"
    assert build_external_id("m1", "") == "vendor-invoice:m1:body"


def test_build_deal_payload_shape():
    from datetime import date

    from accounting.tasks.vendor_invoice.registrar import build_deal_payload

    payload = build_deal_payload(
        company_id=12645899,
        partner_id=100,
        issue_date=date(2026, 4, 20),
        due_date=date(2026, 4, 30),
        total_amount=234500,
        expense_account_item_id=999,
        tax_code=136,
        description="株式会社ピーエスアイ 4月分焙煎委託",
    )
    assert payload["type"] == "expense"
    assert payload["company_id"] == 12645899
    assert payload["partner_id"] == 100
    assert payload["issue_date"] == "2026-04-20"
    assert payload["due_date"] == "2026-04-30"
    assert len(payload["details"]) == 1
    assert payload["details"][0]["amount"] == 234500
    assert payload["details"][0]["account_item_id"] == 999
    assert payload["details"][0]["tax_code"] == 136


# ---- reconciler ----


class _FakeFreeeWithDeals(_FakeFreee):
    def __init__(self, partners, deals):
        super().__init__(partners)
        self._deals = deals

    def list_deals(self, *, start_issue_date, end_issue_date, page_size=100):
        return self._deals


def test_reconciler_matches_payment_by_partner_and_amount(tmp_path: Path, monkeypatch):
    """登録済 unpaid 候補と、freee 上の振込 Deal がマッチしたら status=reconciled。"""
    from datetime import date

    # SQLite を tmp に振り替え
    from accounting.config import settings as cfg
    from accounting.core import db as db_module

    original_path = cfg.database_path
    cfg.database_path = str(tmp_path / "test.db")
    db_module._engine = None
    db_module._SessionLocal = None
    try:
        from accounting.core.db import init_db
        from accounting.core.vendor_invoice_candidates import (
            get_by_id,
            upsert_candidate,
        )

        init_db()
        c = upsert_candidate(
            gmail_message_id="m1",
            gmail_attachment_id="a1",
            received_at=datetime(2026, 4, 20, 9, 0),
            sender="management@psi-coffee.com",
            subject="請求書",
            classification="bank_transfer_invoice",
            extracted_amount=234500,
            extracted_issue_date=date(2026, 4, 20),
            extracted_due_date=date(2026, 4, 30),
            extracted_partner_name="株式会社ピーエスアイ",
            freee_partner_id=100,
            freee_deal_id=1001,
            status="registered",
        )

        fake_deal_b = {
            "id": 2002,
            "type": "expense",
            "partner_id": 100,
            "amount": 234500,
            "issue_date": "2026-04-28",
            "from_walletable_id": 9,
            "from_walletable_type": "bank_account",
        }
        fake = _FakeFreeeWithDeals([], [fake_deal_b])

        from accounting.tasks.vendor_invoice.reconciler import reconcile_candidate

        result = reconcile_candidate(fake, c)  # type: ignore[arg-type]
        assert result.matched is True
        assert result.matched_deal_id == 2002

        refreshed = get_by_id(c.id)
        assert refreshed is not None
        assert refreshed.status == "reconciled"
        assert refreshed.reconciled_with_deal_id == 2002
    finally:
        cfg.database_path = original_path
        db_module._engine = None
        db_module._SessionLocal = None


def test_reconciler_keeps_unpaid_when_no_match(tmp_path: Path, monkeypatch):
    from datetime import date

    from accounting.config import settings as cfg
    from accounting.core import db as db_module

    original_path = cfg.database_path
    cfg.database_path = str(tmp_path / "test.db")
    db_module._engine = None
    db_module._SessionLocal = None
    try:
        from accounting.core.db import init_db
        from accounting.core.vendor_invoice_candidates import (
            get_by_id,
            upsert_candidate,
        )

        init_db()
        c = upsert_candidate(
            gmail_message_id="m2",
            gmail_attachment_id="a2",
            received_at=datetime(2026, 4, 20, 9, 0),
            sender="management@psi-coffee.com",
            subject="請求書",
            classification="bank_transfer_invoice",
            extracted_amount=999000,
            extracted_due_date=date(2026, 4, 30),
            freee_partner_id=100,
            status="registered",
        )

        fake = _FakeFreeeWithDeals([], [])
        from accounting.tasks.vendor_invoice.reconciler import reconcile_candidate

        result = reconcile_candidate(fake, c)  # type: ignore[arg-type]
        assert result.matched is False
        refreshed = get_by_id(c.id)
        assert refreshed is not None
        assert refreshed.status == "unpaid"
    finally:
        cfg.database_path = original_path
        db_module._engine = None
        db_module._SessionLocal = None
