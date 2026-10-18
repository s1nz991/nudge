import json
from datetime import datetime, timedelta, timezone

from openai import APIError, AuthenticationError, NotFoundError, OpenAI, RateLimitError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Fact, Goal, Message, Tracker, User
from app.prompts import SYSTEM_PROMPT

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "remember_fact",
            "description": "Store a durable fact about the person (identity, preference, constraint, context).",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["identity", "preference", "constraint", "context"],
                    },
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["category", "key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_profile",
            "description": "Set their name, timezone (IANA), or preferred daily check-in hour (0-23, their local time).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "timezone": {"type": "string"},
                    "checkin_hour": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_goal",
            "description": "Create or update a goal they care about.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "why": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "paused", "done"]},
                    "cadence": {"type": "string", "enum": ["daily", "weekly"]},
                    "progress_note": {"type": "string"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_tracker",
            "description": "Create a simple personal tool: habit, workout, savings, meal, or custom log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["habit", "workout", "savings", "meal", "custom"],
                    },
                    "title": {"type": "string"},
                    "target": {"type": "string", "description": "e.g. 4x/week, $8000, 8 hours sleep"},
                },
                "required": ["kind", "title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "log_tracker",
            "description": "Record an event on an existing tracker (workout done, money saved, habit completed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "note": {"type": "string"},
                    "amount": {"type": "number"},
                    "completed": {"type": "boolean"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_checkin",
            "description": "When Nudge should text them next. Use hours_from_now for a follow-up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours_from_now": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["hours_from_now"],
            },
        },
    },
]


def _client() -> OpenAI:
    kwargs: dict = {"api_key": settings.openai_api_key or "missing"}
    if settings.openai_base_url:
        kwargs["base_url"] = settings.openai_base_url
    return OpenAI(**kwargs)


def _key_configured() -> bool:
    key = (settings.openai_api_key or "").strip()
    return bool(key) and key not in {"sk-your-key", "missing"}


def _describe_api_error(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, RateLimitError) or "insufficient_quota" in text or "credit_balance" in text:
        return (
            "Your OpenAI account has no credits left, so Nudge cannot reply yet.\n"
            "Add billing at https://platform.openai.com/settings/organization/billing/\n"
            "or switch .env to a free-tier Groq key:\n"
            "  OPENAI_API_KEY=gsk_your_groq_key\n"
            "  OPENAI_BASE_URL=https://api.groq.com/openai/v1\n"
            "  OPENAI_MODEL=qwen/qwen3.8-27b\n"
            "Then restart: python -m app.cli"
        )
    if isinstance(exc, AuthenticationError) or "invalid_api_key" in text:
        return (
            "The API key in .env was rejected. Check OPENAI_API_KEY "
            "(and OPENAI_BASE_URL if you are not using OpenAI)."
        )
    if isinstance(exc, NotFoundError) or "model_not_found" in text:
        return (
            f"Model not found: {settings.openai_model}. "
            "Set OPENAI_MODEL in .env to a model your provider still offers "
            "(Groq example: qwen/qwen3.8-27b), then restart."
        )
    if isinstance(exc, APIError):
        return f"The model API failed ({exc.__class__.__name__}). Try again in a moment."
    return f"Could not reach the model: {exc}"


def _memory_block(db: Session, user: User) -> str:
    facts = db.query(Fact).filter(Fact.user_id == user.id).all()
    goals = db.query(Goal).filter(Goal.user_id == user.id).all()
    trackers = db.query(Tracker).filter(Tracker.user_id == user.id).all()
    lines = [
        f"Name: {user.name or 'unknown'}",
        f"Timezone: {user.timezone}",
        f"Preferred check-in hour (local): {user.checkin_hour}",
        f"Next check-in UTC: {user.next_checkin_at}",
        "Facts:",
    ]
    if facts:
        lines.extend(f"- [{f.category}] {f.key}: {f.value}" for f in facts)
    else:
        lines.append("- (none yet)")
    lines.append("Goals:")
    if goals:
        for g in goals:
            lines.append(f"- {g.title} ({g.status}, {g.cadence}) why={g.why or ''}")
    else:
        lines.append("- (none yet)")
    lines.append("Trackers:")
    if trackers:
        for t in trackers:
            lines.append(f"- {t.title} [{t.kind}] state={t.state_json}")
    else:
        lines.append("- (none yet)")
    return "\n".join(lines)


def _recent_messages(db: Session, user: User, limit: int = 24) -> list[dict]:
    rows = (
        db.query(Message)
        .filter(Message.user_id == user.id)
        .order_by(Message.id.desc())
        .limit(limit)
        .all()
    )
    rows.reverse()
    return [{"role": m.role, "content": m.body} for m in rows]


def _upsert_fact(db: Session, user: User, category: str, key: str, value: str) -> str:
    row = (
        db.query(Fact)
        .filter(Fact.user_id == user.id, Fact.category == category, Fact.key == key)
        .one_or_none()
    )
    if row:
        row.value = value
        row.updated_at = datetime.now(timezone.utc)
    else:
        db.add(Fact(user_id=user.id, category=category, key=key, value=value))
    return f"saved fact {category}/{key}"


def _upsert_goal(db: Session, user: User, args: dict) -> str:
    title = args["title"].strip()
    row = (
        db.query(Goal)
        .filter(Goal.user_id == user.id, Goal.title == title)
        .one_or_none()
    )
    if not row:
        row = Goal(user_id=user.id, title=title)
        db.add(row)
        db.flush()
    if args.get("why"):
        row.why = args["why"]
    if args.get("status"):
        row.status = args["status"]
    if args.get("cadence"):
        row.cadence = args["cadence"]
    if args.get("progress_note"):
        row.last_progress_at = datetime.now(timezone.utc)
        _upsert_fact(db, user, "progress", row.title, args["progress_note"])
    return f"goal '{row.title}' is {row.status}"


def _create_tracker(db: Session, user: User, args: dict) -> str:
    title = args["title"].strip()
    existing = (
        db.query(Tracker)
        .filter(Tracker.user_id == user.id, Tracker.title == title)
        .one_or_none()
    )
    state = {
        "target": args.get("target"),
        "streak": 0,
        "total": 0,
        "logs": [],
    }
    if existing:
        existing.kind = args["kind"]
        data = json.loads(existing.state_json or "{}")
        if args.get("target"):
            data["target"] = args["target"]
        existing.state_json = json.dumps(data)
        return f"updated tracker '{title}'"
    db.add(
        Tracker(
            user_id=user.id,
            kind=args["kind"],
            title=title,
            state_json=json.dumps(state),
        )
    )
    return f"created {args['kind']} tracker '{title}'"


def _log_tracker(db: Session, user: User, args: dict) -> str:
    title = args["title"].strip()
    row = (
        db.query(Tracker)
        .filter(Tracker.user_id == user.id, Tracker.title == title)
        .one_or_none()
    )
    if not row:
        return f"no tracker named '{title}'"
    state = json.loads(row.state_json or "{}")
    logs = state.setdefault("logs", [])
    completed = args.get("completed", True)
    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "note": args.get("note"),
        "amount": args.get("amount"),
        "completed": completed,
    }
    logs.append(entry)
    if completed:
        state["streak"] = int(state.get("streak") or 0) + 1
        state["total"] = float(state.get("total") or 0) + float(args.get("amount") or 1)
    else:
        state["streak"] = 0
    if len(logs) > 60:
        state["logs"] = logs[-60:]
    row.state_json = json.dumps(state)
    goal = db.query(Goal).filter(Goal.user_id == user.id, Goal.title == title).one_or_none()
    if goal:
        goal.last_progress_at = datetime.now(timezone.utc)
    return f"logged on '{title}'; streak={state.get('streak')} total={state.get('total')}"


def _run_tool(db: Session, user: User, name: str, raw_args: str) -> str:
    args = json.loads(raw_args or "{}")
    if name == "remember_fact":
        return _upsert_fact(db, user, args["category"], args["key"], args["value"])
    if name == "set_profile":
        if args.get("name"):
            user.name = args["name"]
        if args.get("timezone"):
            user.timezone = args["timezone"]
        if args.get("checkin_hour") is not None:
            user.checkin_hour = int(args["checkin_hour"])
        return "profile updated"
    if name == "upsert_goal":
        return _upsert_goal(db, user, args)
    if name == "create_tracker":
        return _create_tracker(db, user, args)
    if name == "log_tracker":
        return _log_tracker(db, user, args)
    if name == "schedule_checkin":
        hours = float(args["hours_from_now"])
        user.next_checkin_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        return f"next check-in at {user.next_checkin_at.isoformat()}"
    return f"unknown tool {name}"


def reply_to_user(db: Session, user: User, inbound: str, *, proactive: bool = False) -> str:
    if not _key_configured():
        return (
            "Nudge is running, but OPENAI_API_KEY is missing. "
            "Add it to .env and text again."
        )

    if not proactive:
        db.add(Message(user_id=user.id, role="user", body=inbound))
        db.flush()

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": _memory_block(db, user)},
        *_recent_messages(db, user),
    ]
    if proactive:
        messages.append(
            {
                "role": "system",
                "content": "This is a proactive check-in you are sending first. Be brief. Reference a real goal or tracker if you have one.",
            }
        )

    client = _client()
    try:
        return _complete_reply(db, user, client, messages)
    except Exception as exc:
        db.rollback()
        return _describe_api_error(exc)


def _complete_reply(db: Session, user: User, client: OpenAI, messages: list[dict]) -> str:
    for _ in range(8):
        response = client.chat.completions.create(
            model=settings.openai_model,
            messages=messages,
            tools=TOOLS,
            temperature=0.6,
        )
        choice = response.choices[0].message
        tool_calls = choice.tool_calls or []
        if not tool_calls:
            text = (choice.content or "").strip() or "Still here. What's the move today?"
            db.add(Message(user_id=user.id, role="assistant", body=text))
            if user.next_checkin_at is None:
                user.next_checkin_at = datetime.now(timezone.utc) + timedelta(hours=24)
            db.commit()
            return text

        messages.append(
            {
                "role": "assistant",
                "content": choice.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
        )
        for tc in tool_calls:
            result = _run_tool(db, user, tc.function.name, tc.function.arguments)
            db.commit()
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    fallback = "Got it. Tell me the next small step you want to take."
    db.add(Message(user_id=user.id, role="assistant", body=fallback))
    db.commit()
    return fallback
