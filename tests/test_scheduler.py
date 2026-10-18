"""Tests for app.scheduler — proactive check-in sweep and timezone handling."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from app.models import Message, User
from app.scheduler import due_checkins, run_due_checkins


class TestDueCheckins:
    def test_finds_overdue_user(self, db_session, sample_user):
        """A user whose next_checkin_at is in the past should be returned."""
        sample_user.next_checkin_at = datetime.now(timezone.utc) - timedelta(hours=1)
        db_session.commit()
        result = due_checkins(db_session)
        assert len(result) == 1
        assert result[0].id == sample_user.id

    def test_skips_future_checkin(self, db_session, sample_user):
        """A user whose next_checkin_at is in the future should NOT be returned."""
        sample_user.next_checkin_at = datetime.now(timezone.utc) + timedelta(hours=1)
        db_session.commit()
        result = due_checkins(db_session)
        assert len(result) == 0

    def test_skips_null_checkin(self, db_session, sample_user):
        """A user with next_checkin_at=None should NOT be returned."""
        sample_user.next_checkin_at = None
        db_session.commit()
        result = due_checkins(db_session)
        assert len(result) == 0

    def test_handles_naive_datetime_from_sqlite(self, db_session, sample_user):
        """SQLite returns naive datetimes.  The comparison must still work.

        Simulate by storing a naive past datetime directly (as SQLite would
        return it after a round-trip).
        """
        # Store a naive datetime (no tzinfo), as SQLite actually stores
        naive_past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        sample_user.next_checkin_at = naive_past
        db_session.commit()

        # Refresh to simulate what SQLite gives back
        db_session.expire(sample_user)
        result = due_checkins(db_session)
        assert len(result) == 1

    def test_handles_aware_datetime(self, db_session, sample_user):
        """If the driver somehow preserves tzinfo, comparison still works."""
        aware_past = datetime.now(timezone.utc) - timedelta(hours=2)
        sample_user.next_checkin_at = aware_past
        db_session.commit()

        db_session.expire(sample_user)
        result = due_checkins(db_session)
        assert len(result) == 1


class TestRunDueCheckins:
    @patch("app.scheduler.send_message")
    @patch("app.scheduler.reply_to_user")
    @patch("app.scheduler.SessionLocal")
    def test_sends_proactive_reply(self, mock_session_cls, mock_reply, mock_send):
        """run_due_checkins should call reply_to_user(proactive=True) and send_message."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from app.db import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(bind=engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        user = User(
            phone="+15550000099",
            channel="sms",
            next_checkin_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        db.add(user)
        db.commit()
        db.refresh(user)

        mock_session_cls.return_value = db
        mock_reply.return_value = "How's the running going?"

        count = run_due_checkins()

        assert count == 1
        mock_reply.assert_called_once()
        # Verify proactive=True was passed
        _, kwargs = mock_reply.call_args
        assert kwargs.get("proactive") is True
        mock_send.assert_called_once_with("sms", "+15550000099", "How's the running going?")

        db.close()
        engine.dispose()
