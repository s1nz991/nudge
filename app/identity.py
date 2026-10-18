from datetime import datetime, timedelta, timezone


def parse_sender(raw: str) -> tuple[str, str]:
    value = (raw or "").strip()
    if value.lower().startswith("whatsapp:"):
        return "whatsapp", value.split(":", 1)[1]
    return "sms", value


def outbound_address(channel: str, phone: str) -> str:
    if channel == "whatsapp":
        return f"whatsapp:{phone}"
    return phone


def default_next_checkin(hours: float = 24) -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=hours)
