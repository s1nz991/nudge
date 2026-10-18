"""Tests for the interactive terminal chatbot (app.cli).

All LLM calls are mocked — no real API traffic.
"""

from unittest.mock import MagicMock, patch

import pytest

from app.cli import (
    DEFAULT_CLI_IDENTITY,
    HELP_TEXT,
    chat_loop,
    format_cli_error,
    is_exit_command,
    is_help_command,
    resolve_user,
    run_turn,
)
from app.models import Message, User


class TestCommandHelpers:
    def test_exit_variants(self):
        assert is_exit_command("/exit")
        assert is_exit_command("/QUIT")
        assert is_exit_command("  /quit  ")
        assert not is_exit_command("/help")
        assert not is_exit_command("exit")

    def test_help_command(self):
        assert is_help_command("/help")
        assert is_help_command(" /Help ")
        assert not is_help_command("/exit")


class TestFormatCliError:
    def test_auth_error_is_actionable(self):
        msg = format_cli_error(Exception("Invalid API key provided: sk-abc"))
        assert "OPENAI_API_KEY" in msg
        assert "sk-abc" not in msg

    def test_connection_error(self):
        msg = format_cli_error(ConnectionError("connection refused"))
        assert "reach the model API" in msg

    def test_long_message_truncated(self):
        msg = format_cli_error(RuntimeError("x" * 500))
        assert len(msg) < 250
        assert msg.endswith("...")


class TestResolveUser:
    def test_creates_default_user(self, db_session):
        user = resolve_user(db_session, None)
        assert user.phone == DEFAULT_CLI_IDENTITY
        assert user.channel == "sms"

    def test_reuses_existing_user(self, db_session):
        first = resolve_user(db_session, "+15551212")
        second = resolve_user(db_session, "+15551212")
        assert first.id == second.id
        assert db_session.query(User).count() == 1

    def test_whatsapp_identity(self, db_session):
        user = resolve_user(db_session, "whatsapp:+15559876")
        assert user.channel == "whatsapp"
        assert user.phone == "+15559876"


class TestRunTurn:
    def test_calls_agent_and_updates_last_inbound(self, db_session, sample_user):
        with patch("app.cli.reply_to_user", return_value="Hello from Nudge") as mock_reply:
            reply = run_turn(db_session, sample_user, "hi there")
        assert reply == "Hello from Nudge"
        mock_reply.assert_called_once()
        assert sample_user.last_inbound_at is not None

    def test_agent_exception_becomes_safe_error(self, db_session, sample_user):
        with patch("app.cli.reply_to_user", side_effect=RuntimeError("boom")):
            reply = run_turn(db_session, sample_user, "hi")
        assert "Something went wrong" in reply
        assert "boom" in reply


class TestChatLoop:
    def _collect(self, inputs: list[str], db_session, user, reply_side_effect=None):
        """Drive chat_loop with scripted input; return printed lines."""
        printed: list[str] = []
        queue = iter(inputs)

        def fake_input(prompt: str = "") -> str:
            printed.append(prompt.rstrip("\n"))
            try:
                return next(queue)
            except StopIteration:
                raise EOFError

        reply_kw = {"return_value": "Mocked reply"}
        if reply_side_effect is not None:
            reply_kw = {"side_effect": reply_side_effect}

        with patch("app.cli.reply_to_user", **reply_kw) as mock_reply:
            chat_loop(
                db_session,
                user,
                input_fn=fake_input,
                print_fn=lambda s: printed.append(s),
            )
        return printed, mock_reply

    def test_greeting_and_prompts(self, db_session, sample_user):
        printed, _ = self._collect(["/exit"], db_session, sample_user)
        assert any(line.startswith("Nudge: Hi") for line in printed)
        assert "You: " in printed
        assert any(line == "Nudge: Bye." for line in printed)

    def test_blank_input_skipped(self, db_session, sample_user):
        printed, mock_reply = self._collect(
            ["", "   ", "/exit"], db_session, sample_user
        )
        mock_reply.assert_not_called()
        assert any(line == "Nudge: Bye." for line in printed)

    def test_help_does_not_call_agent(self, db_session, sample_user):
        printed, mock_reply = self._collect(["/help", "/quit"], db_session, sample_user)
        mock_reply.assert_not_called()
        assert any("Commands:" in line for line in printed)
        assert HELP_TEXT.splitlines()[0] in "\n".join(printed)

    def test_message_reaches_agent_and_shows_reply(self, db_session, sample_user):
        printed, mock_reply = self._collect(
            ["I want to run more", "/exit"], db_session, sample_user
        )
        mock_reply.assert_called_once()
        args, _kwargs = mock_reply.call_args
        assert args[2] == "I want to run more"
        assert any(line == "Nudge: Mocked reply" for line in printed)

    def test_multi_turn_calls_agent_each_time(self, db_session, sample_user):
        printed, mock_reply = self._collect(
            ["first", "second", "/exit"],
            db_session,
            sample_user,
            reply_side_effect=["Reply A", "Reply B"],
        )
        assert mock_reply.call_count == 2
        assert any(line == "Nudge: Reply A" for line in printed)
        assert any(line == "Nudge: Reply B" for line in printed)

    def test_ctrl_c_exits_gracefully(self, db_session, sample_user):
        printed: list[str] = []

        def boom(_prompt: str = "") -> str:
            raise KeyboardInterrupt

        with patch("app.cli.reply_to_user") as mock_reply:
            chat_loop(
                db_session,
                sample_user,
                input_fn=boom,
                print_fn=lambda s: printed.append(s),
            )
        mock_reply.assert_not_called()
        assert any(line == "Nudge: Bye." for line in printed)


class TestConversationPersistence:
    """Conversation context is stored via reply_to_user → Message rows."""

    @patch("app.agent._client")
    @patch("app.agent._key_configured", return_value=True)
    def test_turns_persist_messages(
        self, _mock_key, mock_client_fn, db_session, sample_user, mock_openai_client
    ):
        mock_openai_client.chat.completions.create.side_effect = [
            _text_response("Got your first note."),
            _text_response("And I remember the second."),
        ]
        mock_client_fn.return_value = mock_openai_client

        run_turn(db_session, sample_user, "first note")
        run_turn(db_session, sample_user, "second note")

        msgs = (
            db_session.query(Message)
            .filter(Message.user_id == sample_user.id)
            .order_by(Message.id)
            .all()
        )
        assert [m.role for m in msgs] == ["user", "assistant", "user", "assistant"]
        assert msgs[0].body == "first note"
        assert msgs[2].body == "second note"

    @patch("app.agent._client")
    @patch("app.agent._key_configured", return_value=True)
    def test_same_identity_reloads_across_resolve(
        self, _mock_key, mock_client_fn, db_session, mock_openai_client
    ):
        mock_client_fn.return_value = mock_openai_client
        user = resolve_user(db_session, DEFAULT_CLI_IDENTITY)
        run_turn(db_session, user, "remember my goal")

        again = resolve_user(db_session, DEFAULT_CLI_IDENTITY)
        assert again.id == user.id
        count = (
            db_session.query(Message).filter(Message.user_id == again.id).count()
        )
        assert count == 2  # user + assistant from the prior turn


def _text_response(content: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    resp = MagicMock()
    resp.choices = [choice]
    return resp
