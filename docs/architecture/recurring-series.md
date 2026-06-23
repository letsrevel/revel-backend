# Recurring Event Series

Revel models recurring events as **first-class series with rolling-window materialization**. A recurring series is defined by a `RecurrenceRule` and a **template event**; a daily Celery beat task walks the rule forward and materializes each occurrence as a **real, independent `Event`** — its own tickets, payments, capacity, RSVPs, and check-in window. Occurrences are not virtual: once generated, they behave exactly like a one-off event.

This page covers the rule model, how and when occurrences are generated, drift detection after a cadence change, how template edits propagate, and the single `SERIES_EVENTS_GENERATED` digest sent per generated batch.

!!! note "A series is recurring iff it has a rule"
    `EventSeries` is also used as a plain grouping container (a series of unrelated events). A series is "recurring" precisely when it has an attached `RecurrenceRule`; that's what the `is_recurring` annotation (`EventSeriesQuerySet.with_is_recurring`) exposes on list/detail.

---

## Data model

### `RecurrenceRule`

`src/events/models/recurrence_rule.py`. Stores the recurrence parameters and computes an RFC 5545 `RRULE` string on save (via `python-dateutil`).

| Field | Type | Notes |
|---|---|---|
| `frequency` | `Frequency` | `DAILY` / `WEEKLY` / `MONTHLY` / `YEARLY`. |
| `interval` | PositiveInt (default 1) | Every N units of the frequency. |
| `weekdays` | JSON list of ints | Weekly recurrence: `0=Mon … 6=Sun`. |
| `monthly_type` | `MonthlyType`, nullable | `DAY_OF_MONTH` or `NTH_WEEKDAY`. |
| `day_of_month` / `nth_weekday` / `weekday` | int, nullable | Monthly parameters (e.g. "3rd Tuesday" or "the 15th"). |
| `dtstart` | DateTime | Anchor of the series. |
| `until` / `count` | DateTime / int, nullable | Optional boundaries. |
| `rrule_string` | Text, read-only | Recomputed on every save from the structured fields. |
| `timezone` | str (default `UTC`) | Validated IANA zone. `to_rrule()` localizes `dtstart` into this zone, so occurrences **preserve their wall-clock time across DST** (a "Mondays 10:00 Europe/Vienna" rule stays at 10:00 Vienna before and after the switch; the underlying UTC instant shifts by the offset). Default `UTC` is a no-op. |

Validation is delegated to `events/utils/recurrence_validators.py` so the Pydantic input schema and the model `clean()` enforce identical rules. `to_rrule()` builds a `dateutil.rrule`; `get_occurrences(after, before)` returns the occurrence datetimes strictly between two bounds.

### `EventSeries` (recurrence fields)

`src/events/models/event_series.py`.

| Field | Type | Notes |
|---|---|---|
| `template_event` | OneToOne → `Event`, `PROTECT` | The DRAFT blueprint every occurrence is cloned from. |
| `recurrence_rule` | OneToOne → `RecurrenceRule`, `PROTECT` | The cadence. |
| `exdates` | JSON list | UTC-normalized ISO strings of cancelled/excluded occurrence instants. |
| `is_active` | bool | Master switch — `False` pauses generation (existing occurrences untouched). |
| `auto_publish` | bool | When `True`, materialized occurrences are created `OPEN` instead of `DRAFT`. |
| `generation_window_weeks` | PositiveInt (default 8, max 52) | Rolling horizon ahead of "now" to keep materialized. |
| `last_generated_until` | DateTime, nullable | Cursor: how far ahead occurrences have been generated. |

!!! danger "Both recurrence FKs are `PROTECT`"
    `template_event` and `recurrence_rule` use `on_delete=PROTECT` so an admin can't delete the Event or RecurrenceRule out from under a (possibly paid-ticket-bearing) series. Series teardown goes through `EventSeries.delete()`, which nulls both PROTECTing FKs in one UPDATE before cascading to the template and occurrences. The orphaned `RecurrenceRule` row is left behind by design (cleanup belongs in a future service-level teardown helper).

---

## Rolling-window materialization

The single entrypoint is `generate_series_events(series, until_override=None)` in `src/events/service/recurrence_service.py`.

```mermaid
flowchart TD
    Start([generate_series_events]) --> Pre{"is_active?<br/>has rule + template?"}
    Pre -->|No| Empty([return []])
    Pre -->|Yes| Lock[SELECT FOR UPDATE series row<br/>of=self]
    Lock --> Recheck{"still active?<br/>still has rule + template?"}
    Recheck -->|No| Empty
    Recheck -->|Yes| Window["start_from = last_generated_until<br/>or dtstart − 1s<br/>horizon = now + window_weeks"]
    Window --> Cap{"start_from > horizon?"}
    Cap -->|Yes| Clamp[clamp start_from = horizon]
    Cap -->|No| Occ
    Clamp --> Occ[rule.between start_from, horizon]
    Occ --> Loop["for each occurrence dt:<br/>skip if in exdates or existing_starts<br/>else materialize_occurrence"]
    Loop --> Cursor[last_generated_until = horizon]
    Cursor --> Commit[(commit)]
    Commit --> Digest{"any created?"}
    Digest -->|Yes| Notify[notify_series_events_generated<br/>SERIES_EVENTS_GENERATED]
    Digest -->|No| Done([return created])
    Notify --> Done
```

### How it works

- **Cheap pre-check, then a locked re-check.** The function first short-circuits inactive / rule-less series without opening a transaction, then re-fetches inside the lock — the locked state is the source of truth.
- **Series-row lock.** The whole computation runs in one `transaction.atomic()` that begins with `select_for_update(of=("self",))` on the `EventSeries` row. This serializes concurrent callers (e.g. the daily beat fan-out overlapping a manual "Generate now" click); without it, both would compute the same gap, call `duplicate_event` with the same deterministic slug, and race on the `(organization, slug)` unique constraint. (`of=("self",)` is required: a plain `FOR UPDATE` with `select_related` on the nullable rule/template FKs would produce a LEFT JOIN that PostgreSQL rejects for `FOR UPDATE`.)
- **The window.** `start_from` is `last_generated_until` (or `dtstart − 1s` on the first run, so `dtstart` itself is included). `horizon` is `now + generation_window_weeks` (or an explicit `until_override` from manual generation). If `start_from > horizon` (e.g. the window was shrunk), it's clamped so generation can't silently stall.
- **Idempotency.** `existing_starts` (occurrences already in the window) and `exdates` are both skipped, so re-running is safe and produces no duplicates. The `occurrence_index` continues monotonically from the max existing index.
- **Cloning.** `materialize_occurrence` calls `duplicate_event` (deep clone of the template) with the occurrence start, index, and a status override (`OPEN` if `auto_publish`, else the template's DRAFT). The event `schedule`/timeline is copied verbatim from the template.
- **Atomicity of cursor + occurrences.** The materialization loop and the `last_generated_until` bump commit together — a failure on any occurrence rolls back the whole batch and the cursor. The notification dispatch is deliberately **outside** the atomic block so a notification glitch can't roll back successfully-materialized events.

### When it runs

| Trigger | Path |
|---|---|
| Daily beat | `events.generate_recurring_events` (Celery beat, **daily at 04:00 UTC**, migration `0067`) fans out one `events.generate_single_series_events` subtask per active recurring series, so one series failing doesn't block the rest. |
| Series creation | `create_recurring_event_series` runs an initial generation pass immediately. |
| Cadence/window change | `update_series_recurrence` resets `last_generated_until` (see below) so the next pass backfills the new cadence. |
| Manual | `POST .../event-series/{id}/generate` (optional `until` override). |

The per-series subtask retries with exponential backoff **only** on transient DB errors (deadlocks, dropped connections); programming errors (missing template, invalid rule) fail loudly on the first run.

---

## Cursor invalidation on cadence change

`update_series_recurrence` (same module) applies partial updates to the series and/or its rule. When a change would shift the schedule, it resets `last_generated_until = None` so the next generation pass regenerates from `dtstart` rather than waiting for real time to catch up. The cursor is invalidated when:

- `generation_window_weeks` changes (decreases can stall; increases should backfill the new horizon).
- Any of the rule's schedule-defining fields change: `frequency`, `interval`, `weekdays`, `monthly_type`, `day_of_month`, `nth_weekday`, `weekday`, `dtstart`, `until`, `count`, `timezone` (`_RULE_CURSOR_INVALIDATING_FIELDS`).

`pause_series` / `resume_series` flip `is_active` without touching the cursor or existing occurrences.

---

## Drift detection

Changing the cadence does **not** retroactively move occurrences that were already materialized — they keep their old dates. `detect_cadence_drift(series)` returns the IDs of future occurrences that no longer match the current `RRULE`, so the admin UI can highlight stale rows and offer a bulk "cancel stale dates" action.

An occurrence is reported as drifted when it is **all** of:

- `is_template=False` — the template is never an occurrence.
- `start >= now()` — past occurrences are out of scope.
- `status != CANCELLED` — already-cancelled rows don't need another pass.
- `is_modified=False` — manually-edited occurrences were deliberately shifted off-cadence by an organizer (the standard event-edit endpoint sets `is_modified=True`); flagging them would be misleading.

Comparison is **by UTC instant** (both the RRULE output and stored `start` are normalized to UTC before the set-membership check), so a DST shift in a stored timezone doesn't cause a spurious classification. Exdates are subtracted from the expected set so an occurrence sitting on an exdate is correctly reported as drift rather than masked. Exposed via `GET .../event-series/{id}/drift`.

---

## Cancelling a single occurrence

`cancel_occurrence(series, occurrence_date)` (under a series-row lock):

1. Adds the occurrence's UTC-normalized instant to `exdates` (deduplicated, so equivalent instants in different tz representations don't accumulate) — this prevents the date from being re-materialized.
2. If the occurrence was already materialized, flips that `Event` to `CANCELLED` (matching by instant, not wall-clock).

The lock makes concurrent cancellations of different occurrences safe (otherwise a last-writer-wins overwrite would silently drop an exclusion).

---

## Template-change propagation

Editing the template (`update_template`) optionally pushes changes onto future occurrences, controlled by a `propagate` scope query parameter:

| Scope | Effect |
|---|---|
| `NONE` (default) | Only the template is updated; the next generation pass picks up changes for *new* occurrences. |
| `FUTURE_UNMODIFIED` | Apply to future occurrences that haven't been manually edited (`is_modified=False`). |
| `ALL_FUTURE` | Apply to all future occurrences, including manually-edited ones. |

Only fields in the **`PROPAGATABLE_FIELDS`** whitelist are propagated — content/config like `name`, `description`, `visibility`, `event_type`, capacity, toggles, `address`/`location`, `is_open_ended`. Per-occurrence state (`start`/`end`, `status`, slugs, FKs, computed fields) is **never** overwritten.

!!! note "Coupled fields move together"
    `address` and `location` (human-readable text + PostGIS `Point`) are a coupled pair: if either is propagated, the other is pulled from the template too, so an occurrence never ends up with an address that disagrees with its coordinates.

Propagation writes via per-instance `save()` (so `full_clean()` and coupled-field invariants run), suppresses per-event notifications during the batch, and runs inside one atomic block (`update_template` updates the template and propagates atomically, avoiding a half-applied state).

---

## The `SERIES_EVENTS_GENERATED` digest

Per-occurrence `EVENT_OPEN` notifications are **suppressed** during materialization (`suppress_event_notifications()`). Instead, after a successful generation batch with at least one new occurrence, `notify_series_events_generated` sends a **single digest** (`NotificationType.SERIES_EVENTS_GENERATED`) — one per batch, not one per occurrence.

The audience mirrors who would have received `EVENT_OPEN`, **gated by the template's visibility**:

- `STAFF_ONLY` / `PRIVATE` / `UNLISTED` → staff + owners only (the digest deliberately doesn't broadcast non-public occurrences to members/followers).
- `MEMBERS_ONLY` → staff, owners, and active/paused members.
- `PUBLIC` → staff, owners, members, plus org/series followers who opted into new-event notifications.

Each candidate is then filtered through their `NotificationPreference` (users who silenced everything or disabled `SERIES_EVENTS_GENERATED` are dropped). The notification links to the public series route `/events/{org_slug}/series/{series_slug}`. See [Notifications](notifications.md) for the multi-channel dispatch.

---

## Admin API

`OrganizationAdminRecurringEventsController` (`src/events/controllers/organization_admin/recurring_events.py`), mounted under `/organization-admin/{slug}`. Creation requires `create_event`; all other endpoints require `edit_event_series`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/create-recurring-event` | Create rule + series + template, run initial generation. Returns `EventSeriesRecurrenceDetailSchema`. |
| `PATCH` | `/event-series/{id}/template` | Edit the template; `?propagate=none\|future_unmodified\|all_future`. |
| `PATCH` | `/event-series/{id}/recurrence` | Update rule / `auto_publish` / `generation_window_weeks` (resets the cursor when the schedule shifts). |
| `POST` | `/event-series/{id}/cancel-occurrence` | Exclude (and cancel, if materialized) one occurrence. |
| `POST` | `/event-series/{id}/generate` | Manual rolling-window generation (optional `until`). |
| `POST` | `/event-series/{id}/pause` · `/resume` | Toggle `is_active`. |
| `GET` | `/event-series/{id}` | Full admin detail (`EventSeriesRecurrenceDetailSchema`). |
| `GET` | `/event-series/{id}/drift` | Off-cadence future occurrence IDs. |
| `GET` | `/event-series/{id}/template-event` | The template's `EventDetailSchema` (the public detail route filters templates out). |

---

## Related reading

- [Notifications](notifications.md) — the `SERIES_EVENTS_GENERATED` digest channel architecture.
- [Service Layer](service-layer.md) — `recurrence_service.py` follows the function-based pattern with row-locked, atomic generation.
- [Engineering Notes](../engineering-notes.md) — Celery `on_commit` and PgBouncer `.iterator()` gotchas relevant to the generation task.
