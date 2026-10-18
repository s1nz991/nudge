"""Tests for app.agent — tool dispatch, memory block, and the full reply loop."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.models import Fact, Goal, Message, Tracker, User
from app.agent import (
    _run_tool,
    _memory_block,
    _upsert_fact,
    _upsert_goal,
    _create_tracker,
    _log_tracker,
    _recent_messages,
    reply_to_user,
)


# ---------------------------------------------------------------------------
# Tool dispatch: _run_tool
# ---------------------------------------------------------------------------


class TestRunTool:
    def test_remember_fact(self, db_session, sample_user):
        result = _run_tool(
            db_session,
            sample_user,
            "remember_fact",
            json.dumps({"category": "identity", "key": "name", "value": "Siddh"}),
        )
        db_session.commit()
        assert "saved fact" in result
        fact = db_session.query(Fact).filter(Fact.user_id == sample_user.id).one()
        assert fact.value == "Siddh"

    def test_set_profile_name(self, db_session, sample_user):
        result = _run_tool(
            db_session, sample_user, "set_profile",
            json.dumps({"name": "Sid", "timezone": "Asia/Kolkata", "checkin_hour": 8}),
        )
        db_session.commit()
        assert result == "profile updated"
        assert sample_user.name == "Sid"
        assert sample_user.timezone == "Asia/Kolkata"
        assert sample_user.checkin_hour == 8

    def test_upsert_goal(self, db_session, sample_user):
        result = _run_tool(
            db_session, sample_user, "upsert_goal",
            json.dumps({"title": "Run a 5K", "why": "health"}),
        )
        db_session.commit()
        assert "Run a 5K" in result
        goal = db_session.query(Goal).filter(Goal.user_id == sample_user.id).one()
        assert goal.title == "Run a 5K"
        assert goal.status == "active"

    def test_create_tracker(self, db_session, sample_user):
        result = _run_tool(
            db_session, sample_user, "create_tracker",
            json.dumps({"kind": "habit", "title": "Morning run", "target": "4x/week"}),
        )
        db_session.commit()
        assert "created" in result.lower()
        tracker = db_session.query(Tracker).filter(Tracker.user_id == sample_user.id).one()
        state = json.loads(tracker.state_json)
        assert state["target"] == "4x/week"
        assert state["streak"] == 0

    def test_log_tracker(self, db_session, sample_user):
        # Create first
        _run_tool(
            db_session, sample_user, "create_tracker",
            json.dumps({"kind": "habit", "title": "Gym"}),
        )
        db_session.commit()
        # Log
        result = _run_tool(
            db_session, sample_user, "log_tracker",
            json.dumps({"title": "Gym", "completed": True, "note": "leg day"}),
        )
        db_session.commit()
        assert "streak=1" in result
        tracker = db_session.query(Tracker).filter(Tracker.title == "Gym").one()
        state = json.loads(tracker.state_json)
        assert len(state["logs"]) == 1
        assert state["logs"][0]["note"] == "leg day"

    def test_log_tracker_missing(self, db_session, sample_user):
        result = _run_tool(
            db_session, sample_user, "log_tracker",
            json.dumps({"title": "Nonexistent"}),
        )
        assert "no tracker" in result.lower()

    def test_schedule_checkin(self, db_session, sample_user):
        result = _run_tool(
            db_session, sample_user, "schedule_checkin",
            json.dumps({"hours_from_now": 2}),
        )
        db_session.commit()
        assert "next check-in" in result
        # SQLite round-trips may strip tzinfo, so compare as naive UTC
        checkin = sample_user.next_checkin_at
        now = datetime.now(timezone.utc)
        if checkin.tzinfo is None:
            now = now.replace(tzinfo=None)
        assert checkin > now

    def test_unknown_tool(self, db_session, sample_user):
        result = _run_tool(db_session, sample_user, "nonexistent_tool", "{}")
        assert "unknown tool" in result.lower()


# ---------------------------------------------------------------------------
# Fact upsert: insert then overwrite
# ---------------------------------------------------------------------------


class TestUpsertFact:
    def test_insert_new(self, db_session, sample_user):
        result = _upsert_fact(db_session, sample_user, "preference", "diet", "vegetarian")
        db_session.commit()
        assert "saved" in result
        assert db_session.query(Fact).count() == 1

    def test_overwrite_existing(self, db_session, sample_user):
        _upsert_fact(db_session, sample_user, "preference", "diet", "vegetarian")
        db_session.commit()
        _upsert_fact(db_session, sample_user, "preference", "diet", "vegan")
        db_session.commit()
        facts = db_session.query(Fact).filter(Fact.key == "diet").all()
        assert len(facts) == 1
        assert facts[0].value == "vegan"


# ---------------------------------------------------------------------------
# Goal upsert
# ---------------------------------------------------------------------------


class TestUpsertGoal:
    def test_creates_new_goal(self, db_session, sample_user):
        _upsert_goal(db_session, sample_user, {"title": "Learn piano", "why": "fun"})
        db_session.commit()
        goal = db_session.query(Goal).one()
        assert goal.title == "Learn piano"
        assert goal.why == "fun"
        assert goal.status == "active"

    def test_updates_existing_goal(self, db_session, sample_user):
        _upsert_goal(db_session, sample_user, {"title": "Learn piano"})
        db_session.commit()
        _upsert_goal(db_session, sample_user, {"title": "Learn piano", "status": "paused"})
        db_session.commit()
        goals = db_session.query(Goal).all()
        assert len(goals) == 1
        assert goals[0].status == "paused"

    def test_progress_note_stored_as_fact(self, db_session, sample_user):
        """The progress_note bug fix: notes must be persisted, not dropped."""
        _upsert_goal(
            db_session, sample_user,
            {"title": "Run a 5K", "progress_note": "ran 2 miles today"},
        )
        db_session.commit()

        # The note should be stored as a fact
        fact = (
            db_session.query(Fact)
            .filter(Fact.user_id == sample_user.id, Fact.category == "progress")
            .one_or_none()
        )
        assert fact is not None, "progress_note was silently dropped — bug not fixed"
        assert fact.key == "Run a 5K"
        assert fact.value == "ran 2 miles today"

        # last_progress_at should also be set
        goal = db_session.query(Goal).one()
        assert goal.last_progress_at is not None

    def test_progress_note_overwrites_previous(self, db_session, sample_user):
        """Only the most recent note per goal is kept in facts."""
        _upsert_goal(
            db_session, sample_user,
            {"title": "Run a 5K", "progress_note": "ran 1 mile"},
        )
        db_session.commit()
        _upsert_goal(
            db_session, sample_user,
            {"title": "Run a 5K", "progress_note": "ran 3 miles"},
        )
        db_session.commit()
        facts = (
            db_session.query(Fact)
            .filter(Fact.category == "progress", Fact.key == "Run a 5K")
            .all()
        )
        assert len(facts) == 1
        assert facts[0].value == "ran 3 miles"


# ---------------------------------------------------------------------------
# Tracker create / log
# ---------------------------------------------------------------------------


class TestCreateTracker:
    def test_idempotent_create(self, db_session, sample_user):
        _create_tracker(db_session, sample_user, {"kind": "habit", "title": "Meditate"})
        db_session.commit()
        _create_tracker(db_session, sample_user, {"kind": "habit", "title": "Meditate", "target": "daily"})
        db_session.commit()
        trackers = db_session.query(Tracker).all()
        assert len(trackers) == 1
        state = json.loads(trackers[0].state_json)
        assert state["target"] == "daily"


class TestLogTracker:
    def test_streak_increments(self, db_session, sample_user):
        _create_tracker(db_session, sample_user, {"kind": "workout", "title": "Gym"})
        db_session.commit()
        _log_tracker(db_session, sample_user, {"title": "Gym", "completed": True})
        db_session.commit()
        _log_tracker(db_session, sample_user, {"title": "Gym", "completed": True})
        db_session.commit()
        tracker = db_session.query(Tracker).one()
        state = json.loads(tracker.state_json)
        assert state["streak"] == 2
        assert state["total"] == 2

    def test_streak_resets_on_incomplete(self, db_session, sample_user):
        _create_tracker(db_session, sample_user, {"kind": "habit", "title": "Read"})
        db_session.commit()
        _log_tracker(db_session, sample_user, {"title": "Read", "completed": True})
        db_session.commit()
        _log_tracker(db_session, sample_user, {"title": "Read", "completed": False})
        db_session.commit()
        tracker = db_session.query(Tracker).one()
        state = json.loads(tracker.state_json)
        assert state["streak"] == 0

    def test_log_capped_at_60(self, db_session, sample_user):
        _create_tracker(db_session, sample_user, {"kind": "habit", "title": "Water"})
        db_session.commit()
        for i in range(65):
            _log_tracker(db_session, sample_user, {"title": "Water", "completed": True})
        db_session.commit()
        tracker = db_session.query(Tracker).one()
        state = json.loads(tracker.state_json)
        assert len(state["logs"]) == 60


# ---------------------------------------------------------------------------
# Memory block
# ---------------------------------------------------------------------------


class TestMemoryBlock:
    def test_empty_user(self, db_session, sample_user):
        block = _memory_block(db_session, sample_user)
        assert "Name: unknown" in block
        assert "(none yet)" in block

    def test_with_data(self, db_session, sample_user):
        sample_user.name = "Sid"
        db_session.add(Fact(user_id=sample_user.id, category="identity", key="role", value="dev"))
        db_session.add(Goal(user_id=sample_user.id, title="Ship Nudge"))
        db_session.commit()
        block = _memory_block(db_session, sample_user)
        assert "Sid" in block
        assert "[identity] role: dev" in block
        assert "Ship Nudge" in block


# ---------------------------------------------------------------------------
# Recent messages
# ---------------------------------------------------------------------------


class TestRecentMessages:
    def test_returns_list_of_dicts(self, db_session, sample_user):
        db_session.add(Message(user_id=sample_user.id, role="user", body="hi"))
        db_session.add(Message(user_id=sample_user.id, role="assistant", body="hey"))
        db_session.commit()
        msgs = _recent_messages(db_session, sample_user)
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hi"}

    def test_capped_at_limit(self, db_session, sample_user):
        for i in range(30):
            db_session.add(Message(user_id=sample_user.id, role="user", body=f"msg{i}"))
        db_session.commit()
        msgs = _recent_messages(db_session, sample_user, limit=10)
        assert len(msgs) == 10


# ---------------------------------------------------------------------------
# Full reply_to_user with mocked LLM
# ---------------------------------------------------------------------------


class TestReplyToUser:
    @patch("app.agent._client")
    @patch("app.agent._key_configured", return_value=True)
    def test_simple_reply(self, _mock_key, mock_client_fn, db_session, sample_user, mock_openai_client):
        mock_client_fn.return_value = mock_openai_client
        reply = reply_to_user(db_session, sample_user, "I want to run more")
        assert isinstance(reply, str)
        assert len(reply) > 0
        # Should have stored the user message and the reply
        msgs = db_session.query(Message).filter(Message.user_id == sample_user.id).all()
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[0].body == "I want to run more"
        assert msgs[1].role == "assistant"

    @patch("app.agent._client")
    @patch("app.agent._key_configured", return_value=True)
    def test_proactive_no_user_message_stored(self, _mock_key, mock_client_fn, db_session, sample_user, mock_openai_client):
        mock_client_fn.return_value = mock_openai_client
        reply = reply_to_user(db_session, sample_user, "[system] check-in", proactive=True)
        msgs = db_session.query(Message).filter(Message.user_id == sample_user.id).all()
        # Only the assistant reply should be stored, not the system inbound
        assert len(msgs) == 1
        assert msgs[0].role == "assistant"

    def test_missing_api_key(self, db_session, sample_user):
        with patch("app.agent._key_configured", return_value=False):
            reply = reply_to_user(db_session, sample_user, "hello")
            assert "OPENAI_API_KEY" in reply

    @patch("app.agent._client")
    @patch("app.agent._key_configured", return_value=True)
    def test_tool_call_flow(self, _mock_key, mock_client_fn, db_session, sample_user):
        """Simulate the LLM calling remember_fact then giving a text reply."""
        # First response: tool call
        tool_call = MagicMock()
        tool_call.id = "call_001"
        tool_call.function.name = "remember_fact"
        tool_call.function.arguments = json.dumps(
            {"category": "identity", "key": "name", "value": "Sid"}
        )
        resp1 = MagicMock()
        resp1.choices = [MagicMock()]
        resp1.choices[0].message.content = ""
        resp1.choices[0].message.tool_calls = [tool_call]

        # Second response: text reply
        resp2 = MagicMock()
        resp2.choices = [MagicMock()]
        resp2.choices[0].message.content = "Got it, Sid!"
        resp2.choices[0].message.tool_calls = None

        client = MagicMock()
        client.chat.completions.create.side_effect = [resp1, resp2]
        mock_client_fn.return_value = client

        reply = reply_to_user(db_session, sample_user, "My name is Sid")
        assert reply == "Got it, Sid!"
        # Fact should be persisted
        fact = db_session.query(Fact).filter(Fact.key == "name").one()
        assert fact.value == "Sid"
