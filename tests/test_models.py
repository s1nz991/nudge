"""Tests for SQLAlchemy models — CRUD operations against in-memory SQLite."""

import json
from datetime import datetime, timezone

from app.models import Fact, Goal, Message, Tracker, User


class TestUserModel:
    def test_create_user(self, db_session, sample_user):
        fetched = db_session.query(User).filter(User.phone == "+15551234567").one()
        assert fetched.id == sample_user.id
        assert fetched.channel == "sms"
        assert fetched.name is None

    def test_unique_phone(self, db_session, sample_user):
        import sqlalchemy
        dup = User(phone="+15551234567", channel="sms")
        db_session.add(dup)
        with __import__("pytest").raises(sqlalchemy.exc.IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_defaults(self, db_session, sample_user):
        assert sample_user.timezone == "UTC"
        assert sample_user.checkin_hour == 9
        assert sample_user.created_at is not None


class TestMessageModel:
    def test_store_and_retrieve(self, db_session, sample_user):
        db_session.add(Message(user_id=sample_user.id, role="user", body="hello"))
        db_session.add(Message(user_id=sample_user.id, role="assistant", body="hi back"))
        db_session.commit()

        msgs = db_session.query(Message).filter(Message.user_id == sample_user.id).all()
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[1].body == "hi back"


class TestFactModel:
    def test_create_fact(self, db_session, sample_user):
        db_session.add(
            Fact(user_id=sample_user.id, category="identity", key="name", value="Siddh")
        )
        db_session.commit()
        fact = db_session.query(Fact).filter(Fact.user_id == sample_user.id).one()
        assert fact.category == "identity"
        assert fact.value == "Siddh"


class TestGoalModel:
    def test_create_goal(self, db_session, sample_user):
        goal = Goal(user_id=sample_user.id, title="Run a 5K", why="stay healthy")
        db_session.add(goal)
        db_session.commit()
        assert goal.status == "active"
        assert goal.cadence == "daily"

    def test_goal_status_update(self, db_session, sample_user):
        goal = Goal(user_id=sample_user.id, title="Save money")
        db_session.add(goal)
        db_session.commit()
        goal.status = "done"
        db_session.commit()
        assert db_session.query(Goal).filter(Goal.id == goal.id).one().status == "done"


class TestTrackerModel:
    def test_create_tracker_with_json_state(self, db_session, sample_user):
        state = {"target": "4x/week", "streak": 0, "total": 0, "logs": []}
        tracker = Tracker(
            user_id=sample_user.id,
            kind="habit",
            title="Morning run",
            state_json=json.dumps(state),
        )
        db_session.add(tracker)
        db_session.commit()
        loaded = json.loads(tracker.state_json)
        assert loaded["target"] == "4x/week"
        assert loaded["streak"] == 0

    def test_default_empty_state(self, db_session, sample_user):
        tracker = Tracker(user_id=sample_user.id, kind="custom", title="Water intake")
        db_session.add(tracker)
        db_session.commit()
        assert tracker.state_json == "{}"
