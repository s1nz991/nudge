"""Tests for app.identity — channel parsing and address helpers."""

from datetime import datetime, timezone

from app.identity import default_next_checkin, outbound_address, parse_sender


class TestParseSender:
    def test_plain_phone(self):
        channel, phone = parse_sender("+15551234567")
        assert channel == "sms"
        assert phone == "+15551234567"

    def test_whatsapp_prefix(self):
        channel, phone = parse_sender("whatsapp:+15551234567")
        assert channel == "whatsapp"
        assert phone == "+15551234567"

    def test_whatsapp_case_insensitive(self):
        channel, phone = parse_sender("WhatsApp:+15551234567")
        assert channel == "whatsapp"
        assert phone == "+15551234567"

    def test_empty_string(self):
        channel, phone = parse_sender("")
        assert channel == "sms"
        assert phone == ""

    def test_none_input(self):
        channel, phone = parse_sender(None)
        assert channel == "sms"
        assert phone == ""


class TestOutboundAddress:
    def test_sms_passthrough(self):
        assert outbound_address("sms", "+15551234567") == "+15551234567"

    def test_whatsapp_prefix_added(self):
        assert outbound_address("whatsapp", "+15551234567") == "whatsapp:+15551234567"


class TestDefaultNextCheckin:
    def test_returns_future_utc(self):
        result = default_next_checkin()
        assert result.tzinfo is not None
        assert result > datetime.now(timezone.utc)

    def test_custom_hours(self):
        before = datetime.now(timezone.utc)
        result = default_next_checkin(hours=1)
        after = datetime.now(timezone.utc)
        # Should be roughly 1 hour from now (with minute/second zeroed)
        assert result > before
        # Within 2 hours to be safe
        from datetime import timedelta
        assert result < after + timedelta(hours=2)
