"""Shared fixtures for Nudge tests.

Every test session gets its own in-memory SQLite database so the real
data/nudge.db is never touched.
"""

import os
import pytest
from unittest.mock import MagicMock, patch

# Point settings at an in-memory DB *before* any app module is imported.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-real")
os.environ.setdefault("OPENAI_MODEL", "test-model")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import User, Message, Fact, Goal, Tracker  # noqa: F401


@pytest.fixture()
def db_session():
    """Yield a SQLAlchemy session backed by a fresh in-memory SQLite DB."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def sample_user(db_session):
    """Insert and return a minimal User row."""
    from app.identity import default_next_checkin

    user = User(
        phone="+15551234567",
        channel="sms",
        next_checkin_at=default_next_checkin(),
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _make_chat_response(content: str = "Hello!", tool_calls=None):
    """Build a mock OpenAI ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = tool_calls
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.fixture()
def mock_openai_client():
    """Return a mock OpenAI client whose chat.completions.create returns a
    simple text reply (no tool calls) by default."""
    client = MagicMock()
    client.chat.completions.create.return_value = _make_chat_response(
        "Hey! What have you been meaning to work on?"
    )
    return client
