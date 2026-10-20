from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rag.plugins.capabilities import ChatMessage, ChatResult, StreamChunk, ToolCall
from rag.privacy.shield import ALL_KINDS, BANK_KINDS, ShieldedChat, Vault, bank_kinds, find, redact

IBAN = "GB29 NWBK 6016 1331 9268 19"        # the standard documentation example, passes mod 97
CARD = "4111 1111 1111 1111"                # a well-known test number, passes Luhn


def kinds(text: str) -> list[str]:
    return [f.kind for f in find(text)]


def test_real_looking_values_are_found() -> None:
    assert kinds(f"Pay to IBAN {IBAN} today") == ["iban"]
    assert kinds(f"Card number {CARD}.") == ["card"]
    assert kinds("Bank Account Number: 0123456789") == ["account"]
    assert kinds("| Account No | 123-456-7890 |") == ["account"]
    assert kinds("SWIFT code: NWBKGB2L") == ["swift"]
    assert kinds("Sort code 60-16-13 Account 31926819") == ["account", "account"]
    assert kinds("SSN 123-45-6789") == ["ssn"] and kinds("Emirates ID 784-1990-1234567-1") == ["emirates_id"]
    assert kinds("Passport No: N1234567") == ["passport"]


def test_ordinary_numbers_and_words_are_left_alone() -> None:
    for text in ("Table 11: year-end statement (2011) 250,000", "Phone +971 4 123 4567", "Project 20260314 rev 3",
                 "The accounts for 2011 show a surplus", "Account 1 and account 2 are reconciled", "Invoice 4111 1111 1111 1112",
                 "Order ITM-000123 for 4,500,000 units", "GB29 NWBK 6016 1331 9268 18"):   # last: IBAN with a wrong check digit
        assert find(text, BANK_KINDS) == [], text   # (a phone number is not a bank value; it is covered by its own kind)


def test_the_model_sees_placeholders_and_the_reader_gets_the_real_value_back() -> None:
    vault = Vault()
    masked = vault.mask(f"Transfer to {IBAN}, reference. Again {IBAN} and account no: 0123456789.")
    assert IBAN not in masked and "0123456789" not in masked
    assert masked.count("[[IBAN_1]]") == 2 and "[[ACCOUNT_1]]" in masked  # one value, one placeholder
    assert vault.unmask(f"The IBAN is [[IBAN_1]] and the account [[ACCOUNT_1]].") == f"The IBAN is {IBAN} and the account 0123456789."
    assert vault.unmask("[[ACCOUNT_9]]") == "[[ACCOUNT_9]]"   # a placeholder it never issued is left as it is


class _Model:
    def __init__(self, reply: str, pieces: list[str] | None = None, tool: ToolCall | None = None) -> None:
        self.reply, self.pieces, self.tool, self.seen = reply, pieces, tool, []
        self.spec = SimpleNamespace(label="openrouter/x/y")

    async def complete(self, messages, **kw):
        self.seen.append(messages)
        return ChatResult(content=self.reply, tool_calls=[self.tool] if self.tool else [])

    async def stream(self, messages, **kw):
        self.seen.append(messages)
        for piece in self.pieces or [self.reply]:
            yield StreamChunk(delta=piece)
        yield StreamChunk(result=ChatResult(content=self.reply))


def _text(messages) -> str:
    return " ".join(m.content if isinstance(m.content, str) else " ".join(p.get("text", "") for p in m.content) for m in messages)


def test_a_request_to_a_model_never_contains_the_value_and_the_answer_does() -> None:
    model = _Model("The account is [[ACCOUNT_1]].")
    shield = ShieldedChat(model)
    result = asyncio.run(shield.complete([ChatMessage("system", "Answer from the passages."),
                                          ChatMessage("user", "Passage: Bank account number: 0123456789. What is the account?")]))
    assert "0123456789" not in _text(model.seen[0]) and "[[ACCOUNT_1]]" in _text(model.seen[0])
    assert result.content == "The account is 0123456789."
    assert shield.spec.label == "openrouter/x/y"  # attributes of the wrapped model still show through


def test_a_placeholder_split_over_streamed_pieces_is_restored_whole() -> None:
    model = _Model("Use [[ACCOUNT_1]] now", pieces=["Use [", "[ACCOUNT", "_1]", "] now"])
    shield = ShieldedChat(model)

    async def run() -> tuple[str, str]:
        text, final = "", ""
        async for chunk in shield.stream([ChatMessage("user", "Account no: 0123456789")]):
            text += chunk.delta
            if chunk.result:
                final = chunk.result.content
        return text, final

    streamed, final = asyncio.run(run())
    assert streamed == "Use 0123456789 now" and final == "Use 0123456789 now"
    assert "[[" not in streamed


def test_tool_arguments_are_masked_going_out_and_restored_coming_back() -> None:
    call = ToolCall("1", "search", {"query": "payments to [[ACCOUNT_1]]"})
    model = _Model("", tool=call)
    shield = ShieldedChat(model)
    history = [ChatMessage("user", "Account number: 0123456789"),
               ChatMessage("assistant", "", tool_calls=[ToolCall("0", "search", {"query": "0123456789"})])]
    result = asyncio.run(shield.complete(history))
    assert "0123456789" not in str(model.seen[0][1].tool_calls[0].arguments)
    assert result.tool_calls[0].arguments == {"query": "payments to 0123456789"}


def test_a_message_with_an_image_is_passed_through_untouched_for_the_caller_to_vet() -> None:
    model = _Model("ok")
    parts = [{"type": "text", "text": "Account no: 0123456789"}, {"type": "image", "media_type": "image/png", "data": "AAAA"}]
    asyncio.run(ShieldedChat(model).complete([ChatMessage("user", parts)]))
    assert model.seen[0][0].content == parts


def test_redact_removes_the_value_for_good() -> None:
    assert redact(f"IBAN {IBAN}; Account no: 0123456789") == "IBAN [redacted iban]; Account no: [redacted account]"


def test_stored_redaction_clears_text_table_cells_and_ocr_lines() -> None:
    from rag.ingestion.pipeline import redact_pages
    from rag.plugins.capabilities import ParsedPage, ParsedTable, TextLine

    page = ParsedPage(number=1, text="Pay Account no: 0123456789 now", method="text_layer",
                      tables=[ParsedTable(rows=[["Bank", "IBAN"], ["X", IBAN]])], lines=[TextLine(text=f"IBAN {IBAN}")])
    removed = redact_pages([page], ("iban", "account"))
    assert removed == 3 and "0123456789" not in page.text and IBAN not in page.tables[0].rows[1][1] and IBAN not in page.lines[0].text
    assert page.tables[0].rows[1][1] == "[redacted iban]"


def test_the_hub_wraps_cloud_chat_models_but_not_local_ones() -> None:
    from rag.config.settings import Settings
    from rag.plugins.hub import ModelHub

    on = ModelHub(Settings(_env_file=None, privacy_mask_for_models=True, llm_answer="extractive/default"))  # type: ignore[call-arg]
    assert not isinstance(on.get("LLM_ANSWER"), ShieldedChat)  # local: nothing leaves, so nothing to mask
    cloud = ModelHub(Settings(_env_file=None, privacy_mask_for_models=True, openrouter_api_key="sk-test",  # type: ignore[call-arg]
                              llm_answer="openrouter/anthropic/claude-haiku-5.5"))
    assert isinstance(cloud.get("LLM_ANSWER"), ShieldedChat)
    off = ModelHub(Settings(_env_file=None, privacy_mask_for_models=False, openrouter_api_key="sk-test",  # type: ignore[call-arg]
                            llm_answer="openrouter/anthropic/claude-haiku-5.5"))
    assert not isinstance(off.get("LLM_ANSWER"), ShieldedChat)


def test_a_currency_or_other_word_between_the_label_and_the_value_does_not_hide_it() -> None:
    for text in ("Bank Account Number AED: 11307757920001", "Bank Account Number AED | 11307757920001",
                 "Account No. (USD) - 11307757920001", "A/C Number Current: 1130 7757 9200 01",
                 "Account No. : 11307757920001 - AED Account Name : ACME LLC", "Beneficiary Account Number AED/USD: 11307757920001"):
        assert kinds(text) == ["account"], text
    masked = Vault().mask("Bank Account Number AED: 11307757920001")
    assert masked == "Bank Account Number AED: [[ACCOUNT_1]]"


def test_the_wider_label_does_not_start_catching_ordinary_sentences() -> None:
    for text in ("Account balance for 2011 was 250,000 in total", "The account was opened on 12 March 2011 with 5 staff",
                 "Account Name: Acme Trading LLC", "Accounts payable 1,250,000 and receivable 3,400,000",
                 "Bank Name Abu Dhabi Commercial Bank PO Box 45384", "Account manager phone 04 123 4567"):
        assert find(text, BANK_KINDS) == [], text


def test_a_document_with_a_bank_value_in_its_text_or_a_table_cell_is_recognised() -> None:
    from rag.ingestion.pipeline import holds_sensitive_values

    kinds = ("iban", "card", "account")
    in_text = SimpleNamespace(text="Bank account number: 0123456789", tables=[])
    in_cell = SimpleNamespace(text="[[TABLE:0]]", tables=[{"rows": [["Bank Account Number AED", "11307757920001"]]}])
    clean = SimpleNamespace(text="Site report", tables=[{"rows": [["Item", "Qty"], ["Cement", "120"]]}])
    assert holds_sensitive_values([in_text], kinds) and holds_sensitive_values([in_cell], kinds)
    assert not holds_sensitive_values([clean], kinds)


def test_the_number_of_masked_values_is_visible_to_the_metering_layer_during_the_call_only() -> None:
    from rag.privacy.shield import MASKED_VALUES

    seen: list[int | None] = []

    class Probe(_Model):
        async def complete(self, messages, **kw):
            seen.append(MASKED_VALUES.get())
            return await super().complete(messages, **kw)

        async def stream(self, messages, **kw):
            seen.append(MASKED_VALUES.get())
            async for chunk in super().stream(messages, **kw):
                yield chunk

    shield = ShieldedChat(Probe("fine"))
    asyncio.run(shield.complete([ChatMessage("user", f"Account no: 0123456789 and IBAN {IBAN}")]))
    asyncio.run(shield.complete([ChatMessage("user", "nothing sensitive here")]))

    async def streamed() -> None:
        async for _ in shield.stream([ChatMessage("user", "Account no: 0123456789")]):
            pass

    asyncio.run(streamed())
    assert seen == [2, 0, 1]        # two values, none, one
    assert MASKED_VALUES.get() is None  # cleared afterwards: a later call without a shield is not mislabelled


def test_the_report_says_in_words_what_was_found_and_shows_what_a_model_receives() -> None:
    from rag.privacy.report import model_view, phrase, scan_pages

    assert phrase({"account": 1, "iban": 1}) == "1 bank account number and 1 IBAN"
    assert phrase({"account": 3}) == "3 bank account numbers" and phrase({"iban": 1, "card": 2, "account": 1}) == "1 IBAN, 2 card numbers and 1 bank account number"
    assert phrase({}) == ""
    pages = [(1, "[[TABLE:0]]", [{"rows": [["Bank Account Number AED", "11307757920001"], ["IBAN AED", IBAN]]}]), (2, "Site report", [])]
    scan = scan_pages(pages, ("iban", "account"))
    assert scan["found"] == {"account": 1, "iban": 1} and list(scan["pages"]) == [1] and scan["phrase"] == "1 bank account number and 1 IBAN"
    view = model_view([(1, "Bank", "Bank Account Number AED | 11307757920001\nIBAN AED | " + IBAN), (2, "Report", "No numbers here")],
                      ("iban", "account"))
    assert view["chunks"] == 2 and [p["page"] for p in view["pages"]] == [1]
    shown = view["pages"][0]["model_view"]
    assert "[[ACCOUNT_1]]" in shown and "[[IBAN_1]]" in shown and "11307757920001" not in shown and IBAN not in shown


def test_email_addresses_and_phone_numbers_are_found_in_their_usual_forms() -> None:
    assert kinds("Write to accounts@acme-trading.example for invoices") == ["email"]
    assert kinds("Contact: Jane.Doe+billing@example.com.") == ["email"]
    for text in ("Tel: +971 4 123 4567", "Mobile: 050 123 4567", "Call us on +971501234567", "Phone no. 04 123 4567",
                 "WhatsApp: 0501234567", "Fax 02-123-4567", "Office 04 123 4567", "+44 20 7946 0958"):
        assert kinds(text) == ["phone"], text


def test_ordinary_numbers_dates_and_amounts_are_not_taken_for_phone_numbers() -> None:
    for text in ("Table 11: year-end statement 250,000 and 4,500,000", "Issued 2026-10-19 rev 3", "Order ITM-000123 for 4,500 units",
                 "Project 20260314 section 03 30 00", "Page 12 of 45, figure 3.2", "P.O. Box 45384 Abu Dhabi", "Invoice 2025/0012345",
                 "Level 3: 4000 x 3600, area 123.06 sq.m", "Contact the site manager on Monday", "support@ is not an address"):
        assert find(text, ("email", "phone")) == [], text


def test_email_and_phone_are_masked_for_a_model_and_restored_for_the_reader() -> None:
    vault = Vault()
    masked = vault.mask("Reach Mr Ali on +971 50 123 4567 or ali@acme.example. Again ali@acme.example")
    assert "ali@acme.example" not in masked and "123 4567" not in masked
    assert masked.count("[[EMAIL_1]]") == 2 and "[[PHONE_1]]" in masked
    assert vault.unmask("Call [[PHONE_1]] or write [[EMAIL_1]]") == "Call +971 50 123 4567 or write ali@acme.example"


def test_only_bank_and_id_values_keep_a_page_or_a_document_away_from_cloud_models() -> None:
    from rag.ingestion.pipeline import holds_sensitive_values

    letterhead = SimpleNamespace(text="Acme Trading LLC. Tel +971 4 123 4567, info@acme.example", tables=[])
    assert find(letterhead.text) and not holds_sensitive_values([letterhead], bank_kinds(("email", "phone", "iban", "account")))
    assert bank_kinds(("email", "iban", "phone", "account")) == ("iban", "account")


def test_an_iban_that_wraps_at_the_end_of_a_line_is_still_found() -> None:
    text = "Beneficiary IBAN AE07\n0331 2345 6789 0123 456 at the bank"
    found = find(text, ALL_KINDS)
    assert [f.kind for f in found] == ["iban"] and "456" in text[found[0].start:found[0].end]


def test_arabic_account_labels_with_arabic_or_western_digits() -> None:
    for text in ("حساب رقم 1234567890123", "رقم الحساب: ١٢٣٤٥٦٧٨٩٠١٢٣", "رقم الحساب المصرفي 0123456789012"):
        assert [f.kind for f in find(text, ALL_KINDS)] == ["account"], text
    assert find("رقم الصفحة 12", ALL_KINDS) == []


def test_payment_ids_secrets_tax_numbers_and_long_numbers_are_masked() -> None:
    def kinds(text: str) -> list[str]:
        return [f.kind for f in find(text, ALL_KINDS)]

    assert kinds("Payment Reference: rxJduo1XlU57cgw3RuHDrWTz0 | Payment Method: Stripe Card Payments") == ["payment_ref"]
    assert kinds("Payment References: rBenXou5idFfkSIjW7pdLAMuh + r14c2l6GE15HFcND65C9uSIE0") == ["payment_ref", "payment_ref"]
    assert kinds("charge pi_3PabcDEFghij1234567890 for cus_Qabc12345678 with sk_live_51Habcdefghijklmnop") == ["payment_ref"] * 3
    assert kinds("password: Hunter2!x") == ["secret"] and kinds("Authorization: Bearer abcdefghij0123456789abcd") == ["secret"]
    assert kinds("TRN: 100123456700003 and more words") == ["tax_id"]
    assert kinds("reference 1234567890123 for the supplier") == ["long_number"]


def test_ordinary_text_numbers_and_words_are_left_alone() -> None:
    for text in ("in_progress and re_ordering", "Order #1001 total 205.4 AED, Id: 7.3163E+12", "amount 1,234,567,890 and 326685.25",
                 "Payment Method: Stripe Card Payments", "2026-05-13 10:22:24 +0400"):
        assert find(text, ALL_KINDS) == [], text
