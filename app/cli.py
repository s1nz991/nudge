"""Interactive terminal chatbot for Nudge.

Run: python -m app.cli

Uses the same agent, SQLite DB, and identity/memory system as the
Twilio / FastAPI paths. Messages are stored per user so multi-turn
context and cross-session memory keep working.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from app.agent import reply_to_user
from app.db import SessionLocal, init_db
from app.identity import default_next_checkin, parse_sender
from app.models import User

DEFAULT_CLI_IDENTITY = "+10000000000"

HELP_TEXT = """\
Commands:
  /help   Show this help
  /exit   Leave the chat
  /quit   Leave the chat

Type normally to talk with Nudge. Blank lines are ignored.
"""


def resolve_user(db, raw_identity: str | None = None) -> User:
    """Get or create the CLI user from a phone / whatsapp: identity string."""
    raw = (raw_identity or "").strip() or DEFAULT_CLI_IDENTITY
    channel, phone = parse_sender(raw)
    if not phone:
        channel, phone = "sms", DEFAULT_CLI_IDENTITY
    user = db.query(User).filter(User.phone == phone).one_or_none()
    if not user:
        user = User(
            phone=phone,
            channel=channel,
            next_checkin_at=default_next_checkin(),
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    else:
        user.channel = channel
        db.commit()
    return user


def format_cli_error(exc: BaseException) -> str:
    """Return a short actionable error; never echo secrets or .env contents."""
    name = exc.__class__.__name__
    text = str(exc).strip()
    lowered = text.lower()
    if (
        "api_key" in lowered
        or "api key" in lowered
        or "authentication" in lowered
        or "invalid_api_key" in lowered
    ):
        return (
            "API authentication failed. Set a valid OPENAI_API_KEY in .env "
            "(and OPENAI_BASE_URL if you are not using OpenAI), then restart."
        )
    if "connection" in lowered or "timeout" in lowered or "connect" in lowered:
        return f"Could not reach the model API ({name}). Check your network and try again."
    # Avoid dumping long provider payloads that might include request headers/keys
    if len(text) > 180:
        text = text[:177] + "..."
    return f"Something went wrong ({name}): {text}" if text else f"Something went wrong ({name})."


def is_exit_command(text: str) -> bool:
    return text.strip().lower() in {"/exit", "/quit"}


def is_help_command(text: str) -> bool:
    return text.strip().lower() == "/help"


def run_turn(db, user: User, body: str) -> str:
    """Send one user message through the agent and return the reply text."""
    user.last_inbound_at = datetime.now(timezone.utc)
    db.commit()
    try:
        return reply_to_user(db, user, body)
    except Exception as exc:
        db.rollback()
        return format_cli_error(exc)


def chat_loop(
    db,
    user: User,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Run the interactive You: / Nudge: conversation until exit."""
    print_fn("Nudge: Hi — I'm Nudge. Type a message to chat, or /help for commands.")
    print_fn("")

    while True:
        try:
            raw = input_fn("You: ")
        except EOFError:
            print_fn("")
            print_fn("Nudge: Bye.")
            break
        except KeyboardInterrupt:
            print_fn("")
            print_fn("Nudge: Bye.")
            break

        body = raw.strip()
        if not body:
            continue
        if is_exit_command(body):
            print_fn("Nudge: Bye.")
            break
        if is_help_command(body):
            print_fn(HELP_TEXT.rstrip())
            print_fn("")
            continue

        reply = run_turn(db, user, body)
        print_fn(f"Nudge: {reply}")
        print_fn("")


def main() -> None:
    init_db()
    db = SessionLocal()
    try:
        try:
            raw = input(
                "Identity for memory (Enter for local default, or phone / whatsapp:+1...): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nNudge: Bye.")
            return
        user = resolve_user(db, raw or None)
        chat_loop(db, user)
    finally:
        db.close()


if __name__ == "__main__":
    main()
