"""Integration tests for FastAPI endpoints.

Covers /dev/message, /webhooks/twilio, /health, and /dev/checkins.

All tests use an in-memory SQLite database (via StaticPool so the single
connection is shared across SessionLocal() calls within a request) and
mocked external services.  No real messages are sent, no paid APIs are
invoked, and data/nudge.db is never touched.
"""

from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Message, User


@pytest.fixture()
def endpoint_env():
    """Yield (client, session_factory, mock_send_message).

    * client          – FastAPI TestClient wired to in-memory SQLite.
    * session_factory – use to query the test DB and verify side effects.
    * mock_send       – the patched ``send_message`` in app.main so tests
                        can assert calls without sending real texts.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # ── Mock LLM: simple text reply, no tool calls ──────────────
    mock_llm_resp = MagicMock()
    mock_llm_resp.choices = [MagicMock()]
    mock_llm_resp.choices[0].message.content = "Hey! What are you working toward?"
    mock_llm_resp.choices[0].message.tool_calls = None

    mock_openai = MagicMock()
    mock_openai.chat.completions.create.return_value = mock_llm_resp

    with ExitStack() as stack:
        # Redirect all SessionLocal() calls to the in-memory DB
        stack.enter_context(patch("app.main.SessionLocal", TestSession))
        stack.enter_context(patch("app.scheduler.SessionLocal", TestSession))

        # Tables already created above; skip real init_db
        stack.enter_context(patch("app.main.init_db"))

        # Prevent real Twilio sends
        mock_send = stack.enter_context(patch("app.main.send_message"))
        stack.enter_context(patch("app.scheduler.send_message"))

        # Mock the OpenAI / Groq client
        stack.enter_context(patch("app.agent._key_configured", return_value=True))
        stack.enter_context(patch("app.agent._client", return_value=mock_openai))

        # Prevent APScheduler from spawning background threads
        stack.enter_context(
            patch(
                "apscheduler.schedulers.background.BackgroundScheduler",
                MagicMock,
            )
        )

        from app.main import app

        with TestClient(app) as client:
            yield client, TestSession, mock_send


# ── /health ─────────────────────────────────────────────────────


class TestHealth:
    def test_returns_ok(self, endpoint_env):
        client, _, _ = endpoint_env
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Twilio creds are not set in the test environment
        assert data["twilio"] is False


# ── /dev/message ────────────────────────────────────────────────


class TestDevMessage:
    def test_simple_reply(self, endpoint_env):
        client, _, _ = endpoint_env
        resp = client.post(
            "/dev/message",
            json={"from": "+15551234567", "body": "I want to run a 5K"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "reply" in data
        assert isinstance(data["reply"], str)
        assert len(data["reply"]) > 0

    def test_creates_user_on_first_contact(self, endpoint_env):
        client, TestSession, _ = endpoint_env
        client.post("/dev/message", json={"from": "+15559999999", "body": "hi"})

        db = TestSession()
        try:
            user = db.query(User).filter(User.phone == "+15559999999").one_or_none()
            assert user is not None
            assert user.channel == "sms"
            assert user.last_inbound_at is not None
        finally:
            db.close()

    def test_stores_user_and_assistant_messages(self, endpoint_env):
        client, TestSession, _ = endpoint_env
        client.post("/dev/message", json={"from": "+15551111111", "body": "hello"})

        db = TestSession()
        try:
            user = db.query(User).filter(User.phone == "+15551111111").one()
            msgs = (
                db.query(Message)
                .filter(Message.user_id == user.id)
                .order_by(Message.id)
                .all()
            )
            assert len(msgs) == 2
            assert msgs[0].role == "user"
            assert msgs[0].body == "hello"
            assert msgs[1].role == "assistant"
        finally:
            db.close()

    def test_calls_send_message(self, endpoint_env):
        client, _, mock_send = endpoint_env
        client.post("/dev/message", json={"from": "+15552222222", "body": "hey"})

        mock_send.assert_called_once()
        channel, phone, body = mock_send.call_args[0]
        assert channel == "sms"
        assert phone == "+15552222222"
        assert isinstance(body, str)

    def test_whatsapp_sender(self, endpoint_env):
        client, TestSession, mock_send = endpoint_env
        client.post(
            "/dev/message",
            json={"from": "whatsapp:+15553333333", "body": "hi from wa"},
        )

        db = TestSession()
        try:
            user = db.query(User).filter(User.phone == "+15553333333").one()
            assert user.channel == "whatsapp"
        finally:
            db.close()

        mock_send.assert_called_once()
        assert mock_send.call_args[0][0] == "whatsapp"

    def test_default_from_when_missing(self, endpoint_env):
        """'from' defaults to +10000000000 when omitted."""
        client, TestSession, _ = endpoint_env
        resp = client.post("/dev/message", json={"body": "test"})
        assert resp.status_code == 200

        db = TestSession()
        try:
            user = db.query(User).filter(User.phone == "+10000000000").one_or_none()
            assert user is not None
        finally:
            db.close()

    def test_empty_body_accepted(self, endpoint_env):
        client, _, _ = endpoint_env
        resp = client.post("/dev/message", json={"from": "+15554444444"})
        assert resp.status_code == 200

    def test_same_user_reused(self, endpoint_env):
        """Sending twice from the same number reuses the User row."""
        client, TestSession, _ = endpoint_env
        client.post("/dev/message", json={"from": "+15555555555", "body": "first"})
        client.post("/dev/message", json={"from": "+15555555555", "body": "second"})

        db = TestSession()
        try:
            users = db.query(User).filter(User.phone == "+15555555555").all()
            assert len(users) == 1
            msgs = (
                db.query(Message)
                .filter(Message.user_id == users[0].id)
                .order_by(Message.id)
                .all()
            )
            # 2 user messages + 2 assistant replies
            assert len(msgs) == 4
        finally:
            db.close()


# ── /webhooks/twilio ────────────────────────────────────────────


class TestTwilioWebhook:
    def test_returns_empty_twiml(self, endpoint_env):
        client, _, _ = endpoint_env
        resp = client.post(
            "/webhooks/twilio",
            data={"From": "+15556666666", "Body": "hello twilio"},
        )
        assert resp.status_code == 200
        assert "text/xml" in resp.headers["content-type"]
        assert "<Response></Response>" in resp.text

    def test_creates_user_via_background_task(self, endpoint_env):
        client, TestSession, _ = endpoint_env
        client.post(
            "/webhooks/twilio",
            data={"From": "+15557777777", "Body": "hey"},
        )

        db = TestSession()
        try:
            user = db.query(User).filter(User.phone == "+15557777777").one_or_none()
            assert user is not None
            assert user.channel == "sms"
        finally:
            db.close()

    def test_whatsapp_from_prefix(self, endpoint_env):
        client, TestSession, mock_send = endpoint_env
        client.post(
            "/webhooks/twilio",
            data={"From": "whatsapp:+15558888888", "Body": "wa msg"},
        )

        db = TestSession()
        try:
            user = db.query(User).filter(User.phone == "+15558888888").one_or_none()
            assert user is not None
            assert user.channel == "whatsapp"
        finally:
            db.close()

    def test_sends_reply_in_background(self, endpoint_env):
        client, _, mock_send = endpoint_env
        client.post(
            "/webhooks/twilio",
            data={"From": "+15550000001", "Body": "hi"},
        )
        mock_send.assert_called_once()
        channel, phone, body = mock_send.call_args[0]
        assert channel == "sms"
        assert phone == "+15550000001"

    def test_stores_messages(self, endpoint_env):
        client, TestSession, _ = endpoint_env
        client.post(
            "/webhooks/twilio",
            data={"From": "+15550000003", "Body": "testing"},
        )

        db = TestSession()
        try:
            user = db.query(User).filter(User.phone == "+15550000003").one()
            msgs = (
                db.query(Message)
                .filter(Message.user_id == user.id)
                .order_by(Message.id)
                .all()
            )
            assert len(msgs) == 2
            assert msgs[0].role == "user"
            assert msgs[0].body == "testing"
            assert msgs[1].role == "assistant"
        finally:
            db.close()

    def test_invalid_signature_rejected(self, endpoint_env):
        """When signature validation is active and fails, return 403."""
        client, _, _ = endpoint_env
        with patch("app.main._valid_twilio", return_value=False):
            resp = client.post(
                "/webhooks/twilio",
                data={"From": "+15550000002", "Body": "spoofed"},
            )
            assert resp.status_code == 403
            assert "invalid signature" in resp.text

    def test_valid_signature_accepted(self, endpoint_env):
        """When signature validation passes, process normally."""
        client, _, mock_send = endpoint_env
        with patch("app.main._valid_twilio", return_value=True):
            resp = client.post(
                "/webhooks/twilio",
                data={"From": "+15550000004", "Body": "legit"},
            )
            assert resp.status_code == 200
            mock_send.assert_called_once()


# ── /dev/checkins ───────────────────────────────────────────────


class TestDevCheckins:
    def test_returns_sent_count(self, endpoint_env):
        client, _, _ = endpoint_env
        resp = client.post("/dev/checkins")
        assert resp.status_code == 200
        data = resp.json()
        assert "sent" in data
        assert isinstance(data["sent"], int)

    def test_sends_to_due_users(self, endpoint_env):
        """Create a user whose check-in is overdue, then trigger the sweep."""
        from datetime import datetime, timedelta, timezone

        client, TestSession, _ = endpoint_env

        # Seed a user with next_checkin_at in the past
        db = TestSession()
        try:
            user = User(
                phone="+15550000010",
                channel="sms",
                next_checkin_at=datetime.now(timezone.utc) - timedelta(hours=1),
            )
            db.add(user)
            db.commit()
        finally:
            db.close()

        # run_due_checkins uses app.scheduler.send_message (already patched
        # in the fixture).  Patch it again here to get a fresh reference
        # for assertion.
        with patch("app.scheduler.send_message") as sched_send:
            resp = client.post("/dev/checkins")
            assert resp.json()["sent"] == 1
            sched_send.assert_called_once()
