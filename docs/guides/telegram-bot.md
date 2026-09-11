# Telegram Bot

The `telegram` app (`src/telegram/`) integrates Revel with Telegram, providing account linking, event interactions, and organizer notifications.

---

## Overview

```mermaid
flowchart LR
    subgraph Telegram
        U[User] <--> Bot[Revel Bot]
    end

    subgraph Revel Backend
        Bot <--> LP[Long-Polling Handler]
        LP <--> R[Routers]
        R <--> SVC[Services Layer]
        NS[Notification System] --> Bot
    end
```

## Core Features

### Account Linking via OTP

Users link their Telegram account to their Revel account using a one-time password (OTP):

1. User sends `/connect` to the bot
2. Bot generates a 9-digit OTP code and displays it to the user
3. User enters the OTP code in the Revel web app
4. The web app validates the code and links the accounts

Once linked, the user can receive notifications and interact with events directly from Telegram.

### Inline Keyboards

User interactions are driven by inline keyboard buttons rather than free-text input, providing a guided experience. Organizers receive action buttons on notifications (e.g., approve/reject invitation requests, approve/reject whitelist requests).

### Notification Delivery

The bot integrates with Revel's notification system to deliver messages to users and organizers via Celery tasks:

- Event updates and reminders
- RSVP and ticket confirmations
- Questionnaire submissions requiring review
- Invitation and whitelist request actions

### Event Interactions

Users can interact with events directly from Telegram callback queries:

- RSVP to events
- Request invitations
- Join waitlists
- Request membership

### Superuser Broadcast

Superusers can broadcast messages to all bot users via a dedicated FSM flow.

---

## Technical Details

| Aspect | Detail |
|---|---|
| **Location** | `src/telegram/` |
| **Framework** | [aiogram](https://docs.aiogram.dev/) |
| **Update mode** | Long-polling (not webhooks) |
| **State management** | Minimal FSM via aiogram (broadcast flow) |
| **Authentication** | Telegram user ID mapped to Revel accounts via `TelegramUser` model |
| **Notifications** | Delivered via Celery tasks for async processing |
| **Commands** | `/start`, `/connect`, `/cancel`, `/unsubscribe` (plus hidden handlers: `/toc`, `/privacy`) |
| **Management command** | `python src/manage.py run_telegram_bot` (or `make run-telegram`) |

### Bot Commands

| Command | Description |
|---|---|
| `/start` | Welcome message, shows linked status |
| `/connect` | Link Telegram account to Revel via OTP |
| `/cancel` | Cancel current FSM conversation |
| `/toc` | Terms and conditions |
| `/privacy` | Privacy policy |
| `/unsubscribe` | Turn off all Telegram notifications |

### Configuration

| Setting | Default | Description |
|---|---|---|
| `FEATURE_TELEGRAM` | `True` (on) | Master switch for the integration. When off, the OTP-linking endpoints (`/telegram/connect`, `/disconnect`, `/status`, `/botname`) return **404** and the notification dispatcher strips the Telegram channel platform-wide, so **no Telegram notifications are sent**. Key self-hosting toggle for instances running without a bot. |
| `TELEGRAM_BOT_TOKEN` | `0000000000:AABBCCDD` (placeholder) | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_OTP_EXPIRATION_MINUTES` | `15` | How long an OTP code is valid for account linking |
| `AIOGRAM_REDIS_DB` | `1` | Redis database index used for aiogram FSM state. The one Redis instance is split by index — `0` Celery broker (`CELERY_REDIS_DB`), `1` aiogram FSM, `2` Django cache (`CACHE_REDIS_DB`). Keep them disjoint: a `FLUSHDB` on one index (which is what `cache.clear()` compiles to) takes out everything sharing it. |
| `AIOGRAM_FSM_TTL_SECONDS` | `86400` (24h) | TTL on FSM state/data keys. Redis runs with `maxmemory-policy volatile-lru`, which can only evict keys that carry a TTL. A user whose state expires mid-flow simply restarts it. |

Access control is **not** configured via environment variables: broadcast powers come from
`RevelUser.is_superuser`, and organizer actions from the acting user's `OrganizationStaff`
permissions on the target organization (see `src/telegram/middleware.py`).

!!! note "Active Development"
    The Telegram bot is an active area of development. Features and conversation flows are being expanded. Refer to the [GitHub issues](https://github.com/letsrevel/revel-backend/issues) for planned work. Check the source code in `src/telegram/` for the most current implementation details.
