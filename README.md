# Nudge

A personal AI that lives in your texts — your own take on [Tomo](https://www.tomo.ai/). It remembers what you are working toward, texts you first, and spins up simple trackers (habits, workouts, savings, meals) from the conversation.

Primary local experience: an **interactive terminal chatbot**. Channel for SMS/WhatsApp via Twilio is optional. There is also a `/dev/message` endpoint for HTTP testing.

## What it does

- **Remembers** name, goals, constraints, and preferences across conversations
- **Checks in** on a schedule the model can set (`schedule_checkin`)
- **Builds tools in-chat**: streak / workout / savings / meal logs you update by texting
- Treats **WhatsApp** the same as SMS (`whatsapp:+1555…` senders)

## Terminal chatbot (Windows PowerShell)

### 1. Install dependencies

```powershell
cd C:\Users\siddh\nudge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

### 2. Configure the environment

```powershell
copy .env.example .env
```

Edit `.env` and set a model key. OpenAI only works if that account has **billing credits**. If you see `credit_balance_exhausted` / 429, either add credits at [OpenAI billing](https://platform.openai.com/settings/organization/billing/) or use Groq (OpenAI-compatible, free tier):

```
OPENAI_API_KEY=gsk_your_groq_key
OPENAI_BASE_URL=https://api.groq.com/openai/v1
OPENAI_MODEL=qwen/qwen3.8-27b
```

Get a Groq key at https://console.groq.com/keys.

Never commit `.env`. The CLI never prints API keys or `.env` contents.

### 3. Launch the chatbot

```powershell
cd C:\Users\siddh\nudge
.\.venv\Scripts\Activate.ps1
python -m app.cli
```

You will see a short identity prompt (press Enter for the local default so memory persists across sessions), then:

```
Nudge: Hi — I'm Nudge. Type a message to chat, or /help for commands.

You:
```

Type normally to talk. Commands:

| Command | Action |
|---------|--------|
| `/help` | Show commands |
| `/exit` or `/quit` | Leave the chat |

Blank lines are ignored. Ctrl+C also exits cleanly.

Conversation history and facts are stored in SQLite (`data/nudge.db`) for the identity you chose, so context carries across turns and later sessions.

### 4. Run tests

```powershell
cd C:\Users\siddh\nudge
.\.venv\Scripts\Activate.ps1
pytest
```

Tests mock the model API; they do not require a real key or network.

## HTTP API (optional)

```powershell
uvicorn app.main:app --reload
```

```powershell
curl -X POST http://127.0.0.1:8000/dev/message -H "Content-Type: application/json" -d "{\"from\":\"+15551212\",\"body\":\"I want to run a 5K and stop skipping gym\"}"
```

## Twilio SMS / WhatsApp

1. Buy or use a Twilio number (enable WhatsApp on that number if you want both).
2. Set `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` in `.env`.
3. Expose the app (`ngrok http 8000`) and set `PUBLIC_BASE_URL` to that https origin.
4. Point the number's messaging webhook to `POST https://<host>/webhooks/twilio`.
5. Text the number. Nudge replies asynchronously (empty TwiML, then REST send) so Twilio does not time out while the model thinks.

WhatsApp sandbox: inbound `From` looks like `whatsapp:+15551212`. Nudge stores the phone and sends back on the same channel.

## How check-ins work

A background job every `CHECKIN_POLL_SECONDS` (default 60) finds users whose `next_checkin_at` is due and texts first. The model should call `schedule_checkin` when it promises a follow-up. New users get a 24h default.

Force a pass: `POST /dev/checkins`.

## Layout

```
app/
  main.py        FastAPI + Twilio webhook
  agent.py       LLM + memory/goal/tracker tools
  scheduler.py   Proactive outbound
  messaging.py   Twilio send (prints to console if unset)
  cli.py         Interactive terminal chatbot
  models.py      SQLite via SQLAlchemy
tests/           pytest suite (mocked APIs)
```

SQLite lives in `data/nudge.db`.
