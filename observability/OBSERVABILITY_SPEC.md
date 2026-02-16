# Observability Implementation Specification

## ⚠️ IMPORTANT: Pyroscope Profiling Disabled

**Status**: Pyroscope continuous profiling is **currently disabled** due to SDK incompatibility.

**Issue**: The `pyroscope-io` Python SDK (v0.8.11) is incompatible with Grafana Pyroscope server v1.6+. The legacy SDK uses a different ingestion protocol than the new Grafana Pyroscope architecture (introduced in v1.0).

**Symptoms**:
- SDK initializes successfully
- Profiling samples are collected locally
- **Data is never sent to the server** (no HTTP requests to `/ingest`)
- Pyroscope UI shows no application profiles

**Root Cause**: Breaking protocol changes in Grafana Pyroscope v1.0+ (July 2023). The new architecture uses a completely different storage format and ingestion API that is incompatible with the legacy `pyroscope-io` SDKs.

**Workarounds**:
1. **Ad-hoc profiling with py-spy** (recommended for now):
   ```bash
   sudo .venv/bin/py-spy record -o flamegraph.svg --duration 60 --pid <DJANGO_PID>
   ```
2. **Downgrade Pyroscope server** to pre-1.0 (loses new features):
   ```yaml
   pyroscope:
     image: pyroscope/pyroscope:0.37.2  # Last pre-1.0 version
   ```
3. **Wait for Grafana** to release an updated Python SDK compatible with v1.6+

**Investigation Summary**: Tested extensively with Python 3.13, confirmed SDK profiling works (py-spy attaches successfully, samples collected) but HTTP ingestion is completely broken. Also tested Grafana Alloy with eBPF profiling, but this doesn't work on Docker for Mac due to PID namespace isolation.

**References**:
- [Grafana Pyroscope Issue #3287](https://github.com/grafana/pyroscope/issues/3287) - Python 3.12+ support
- [Grafana Pyroscope v1.0 Release Notes](https://grafana.com/docs/pyroscope/latest/release-notes/v1-0/) - Breaking changes

---

## Executive Summary

Implement comprehensive observability for the Revel platform using the **LGTM stack** (Loki, Grafana, Tempo, Mimir) ~~+ **Pyroscope** for continuous profiling and flamegraphs~~. All components are self-hostable and open source, with **structlog** as the foundation for structured logging.

**Note**: Pyroscope profiling is temporarily disabled (see warning above).

---

## Core Principles

1. **Self-hostable open source stack** - No vendor lock-in, full data ownership
2. **Structlog foundation** - Structured logging with rich context
3. **Comprehensive telemetry** - Logs, traces, metrics, and profiles unified in Grafana
4. **Flamegraphs** - Continuous profiling with Pyroscope for performance analysis
5. **Privacy-first** - PII scrubbing and sensitive data handling
6. **Environment-aware** - Different configurations for dev/staging/production

---

## Technology Stack

### 1. **Loki** - Log Aggregation
- Scalable log aggregation with label-based indexing
- Cost-effective storage (indexes labels, not full text)
- Native integration with structlog JSON output
- Query language (LogQL) similar to PromQL
- 30-day retention

### 2. **Tempo** - Distributed Tracing
- OpenTelemetry-compatible distributed tracing
- Automatic trace-to-log correlation via trace IDs
- Samples complex request flows across services
- Integration with Django, Celery, external APIs (Stripe, LLM providers)
- 30-day retention

### 3. **Prometheus + Mimir** - Metrics
- Industry-standard time-series metrics database
- Long-term storage with Grafana Mimir
- Django, PostgreSQL, Redis, Celery exporters
- Custom business metrics (events, tickets, revenue)
- 30-day retention (Mimir for long-term)

### 4. ~~**Pyroscope** - Continuous Profiling~~ (DISABLED)
- **CURRENTLY DISABLED** due to SDK incompatibility (see warning at top)
- ~~**Flamegraph generation** for CPU, memory, I/O profiling~~
- ~~Minimal overhead (~1-2% CPU)~~
- ~~Python profiling via `pyroscope-io` SDK~~
- ~~Integrated with Grafana for unified dashboards~~
- ~~Identifies performance bottlenecks and memory leaks~~
- Use **py-spy** for ad-hoc profiling instead

### 5. **Grafana** - Unified Visualization
- Single pane of glass for all telemetry
- Pre-built dashboards for Django, PostgreSQL, Redis, Celery
- Custom business dashboards
- Alerting and notification system
- Correlation between logs, traces, metrics, and profiles

---

## Sampling Strategy

### Development
- **Traces**: 100% sampling
- **Profiling**: Continuous profiling at 100 Hz
- **Logs**: All levels (DEBUG and above)
- **Metrics**: 15s scrape interval

### Production
- **Traces**: 10% sampling (adjustable based on traffic)
- **Profiling**: Continuous profiling at 100 Hz (low overhead)
- **Logs**: INFO and above (DEBUG disabled)
- **Metrics**: 15s scrape interval

---

## Data Retention

All environments:
- **Logs**: 30 days
- **Traces**: 30 days
- **Metrics**: 30 days (with Mimir for long-term storage)
- **Profiles**: 30 days

---

## Sensitive Data Handling

### PII Scrubbing via Structlog Processor

**Fields to redact:**
- `password`, `password_confirmation`, `old_password`
- `card_number`, `cvv`, `card_cvc`
- `ssn`, `social_security_number`
- `address` (optional - discuss per use case)
- `phone_number` (optional - discuss per use case)
- Email addresses in free-text fields (via regex)

**Fields to keep:**
- `user_id` (internal ID, non-PII)
- `organization_id`, `event_id`, `ticket_id`, `payment_id` (internal IDs)
- `request_id`, `trace_id` (correlation IDs)

**Implementation:**
Custom structlog processor that sanitizes before serialization:
```python
def scrub_pii(logger, method_name, event_dict):
    sensitive_keys = ['password', 'card_number', 'cvv', 'ssn']
    for key in sensitive_keys:
        if key in event_dict:
            event_dict[key] = '[REDACTED]'
    # Scrub email patterns from free text
    if 'message' in event_dict:
        event_dict['message'] = re.sub(r'\b[\w\.-]+@[\w\.-]+\.\w+\b', '[EMAIL]', event_dict['message'])
    return event_dict
```

---

## Environment Configuration

### Development (`DEBUG=True`)
- ✅ All observability enabled by default
- ✅ 100% trace sampling
- ✅ DEBUG logs enabled
- ✅ Optional disable via `ENABLE_OBSERVABILITY=False` env var for performance testing

### Staging
- ✅ All observability enabled
- ⚠️ 10% trace sampling (configurable)
- ℹ️ INFO logs and above
- ✅ Full profiling enabled

### Production
- ✅ All observability enabled
- ⚠️ 10% trace sampling (configurable)
- ℹ️ INFO logs and above
- ✅ Full profiling enabled
- 🚨 Alert rules active

### Testing (`pytest`)
- ❌ Observability **disabled** by default for performance
- ✅ Can be enabled via `ENABLE_OBSERVABILITY=True` for integration tests

---

## Custom Business Metrics

### Event Metrics
- `revel_events_created_total` (counter) - Labels: `organization_id`, `event_type` (public/private/members-only), `has_tickets` (true/false)
- `revel_events_active` (gauge) - Currently active (OPEN) events
- `revel_events_cancelled_total` (counter) - Labels: `organization_id`, `cancellation_reason`

### Ticket Metrics
- `revel_tickets_sold_total` (counter) - Labels: `organization_id`, `event_id`, `tier_id`, `payment_method` (online/offline/free/at_door)
- `revel_tickets_checked_in_total` (counter) - Labels: `organization_id`, `event_id`
- `revel_tickets_cancelled_total` (counter) - Labels: `organization_id`, `event_id`, `reason`
- `revel_ticket_tiers_by_type` (counter) - Labels: `organization_id`, `payment_method`

### Payment Metrics
- `revel_payments_total` (counter) - Labels: `organization_id`, `status` (pending/succeeded/failed/refunded), `payment_method`
- `revel_revenue_total` (counter) - Labels: `organization_id`, `currency`
- `revel_platform_fees_total` (counter) - Labels: `organization_id`, `currency`
- `revel_payment_checkout_sessions_created_total` (counter)
- `revel_payment_checkout_sessions_expired_total` (counter)
- `revel_stripe_webhooks_processed_total` (counter) - Labels: `webhook_type`, `status` (success/failure)

### User Metrics
- `revel_users_registered_total` (counter) - Labels: `registration_method` (email/google_sso)
- `revel_users_active_weekly` (gauge) - Users active in last 7 days

### Questionnaire Metrics
- `revel_questionnaires_created_total` (counter) - Labels: `organization_id`, `evaluation_mode` (automatic/manual/hybrid)
- `revel_questionnaire_submissions_total` (counter) - Labels: `organization_id`, `questionnaire_id`, `status` (pending/approved/rejected)
- `revel_questionnaire_evaluations_total` (counter) - Labels: `organization_id`, `evaluation_mode`, `result` (approved/rejected)
- `revel_questionnaire_pass_rate` (gauge) - Labels: `organization_id`, `questionnaire_id`
- `revel_llm_evaluation_duration_seconds` (histogram) - Time to evaluate via LLM
- `revel_llm_tokens_consumed_total` (counter) - Labels: `model`, `evaluation_backend`
- `revel_llm_evaluation_cost_total` (counter) - Labels: `model`, `currency`

### Email/Notification Metrics
- `revel_emails_sent_total` (counter) - Labels: `email_type` (confirmation/notification/invitation), `status` (success/failure)
- `revel_email_delivery_rate` (gauge) - Success rate over last hour
- `revel_notifications_sent_total` (counter) - Labels: `notification_type`, `channel` (email/telegram)

### Membership Metrics
- `revel_organization_members_total` (gauge) - Labels: `organization_id`
- `revel_membership_requests_total` (counter) - Labels: `organization_id`, `status` (pending/approved/rejected)

### System Metrics
- `revel_api_requests_total` (counter) - Labels: `method`, `endpoint`, `status_code`
- `revel_api_request_duration_seconds` (histogram) - Labels: `method`, `endpoint`
- `revel_database_query_duration_seconds` (histogram) - Labels: `query_type`
- `revel_celery_tasks_total` (counter) - Labels: `task_name`, `status` (success/failure/retry)
- `revel_celery_task_duration_seconds` (histogram) - Labels: `task_name`
- `revel_celery_queue_depth` (gauge) - Labels: `queue_name`

---

## Context Enrichment Strategy (Hybrid Approach)

### 1. Middleware-Based Enrichment (HTTP Requests)

**Django middleware** automatically binds request context to structlog:

```python
# src/common/middleware.py
class StructlogContextMiddleware:
    """Enriches structlog context with request metadata."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        import structlog
        import uuid

        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Clear previous context
        structlog.contextvars.clear_contextvars()

        # Bind request context
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            user_id=str(request.user.id) if request.user.is_authenticated else None,
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT'),
            method=request.method,
            path=request.path,
            endpoint=request.resolver_match.view_name if request.resolver_match else None,
        )

        # Add request_id to response headers for client-side correlation
        response = self.get_response(request)
        response['X-Request-ID'] = request_id

        # Clear context after request
        structlog.contextvars.clear_contextvars()

        return response
```

**Automatic context includes:**
- `request_id` - Unique ID for correlation
- `user_id` - Authenticated user ID (if logged in)
- `ip_address` - Client IP address
- `user_agent` - Client user agent
- `method` - HTTP method (GET, POST, etc.)
- `path` - Request path
- `endpoint` - Django view name

### 2. Celery Task Enrichment

**Celery signal handler** binds task context:

```python
# src/revel/celery.py
from celery.signals import task_prerun, task_postrun

@task_prerun.connect
def celery_task_prerun(task_id, task, *args, **kwargs):
    import structlog
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        task_id=task_id,
        task_name=task.name,
        queue=task.request.delivery_info.get('routing_key', 'default'),
        retries=task.request.retries,
    )

@task_postrun.connect
def celery_task_postrun(*args, **kwargs):
    import structlog
    structlog.contextvars.clear_contextvars()
```

**Automatic context includes:**
- `task_id` - Celery task ID
- `task_name` - Task name (e.g., `events.tasks.notify_event_open`)
- `queue` - Queue name (default/telegram/maintenance/pdf)
- `retries` - Number of retries attempted

### 3. Domain-Specific Context (Manual Binding)

**Critical operations** explicitly bind domain context:

#### Payment Operations
```python
# In StripeService.create_checkout_session()
logger.info(
    "stripe_checkout_session_created",
    payment_id=payment.id,
    ticket_id=ticket.id,
    organization_id=organization.id,
    event_id=event.id,
    amount_cents=amount_cents,
    currency=currency,
    stripe_session_id=session.id,
)
```

#### Questionnaire Evaluation
```python
# In SubmissionEvaluator.evaluate()
logger.info(
    "questionnaire_evaluation_started",
    submission_id=submission.id,
    questionnaire_id=questionnaire.id,
    organization_id=questionnaire.organization_id,
    evaluation_mode=questionnaire.evaluation_mode,
    evaluator_backend=evaluator.__class__.__name__,
)
```

#### Event Lifecycle
```python
# In Event.publish()
logger.info(
    "event_published",
    event_id=self.id,
    organization_id=self.organization_id,
    event_type=self.get_event_type(),
    has_tickets=self.ticket_tiers.exists(),
)
```

### Log Levels

- **DEBUG**: Detailed trace information (only in development)
- **INFO**: Business events, successful operations
- **WARNING**: Anomalies, fallback behaviors, soft failures
- **ERROR**: Failures requiring attention, exceptions
- **CRITICAL**: System-level failures, data corruption

---

## Dependencies

### Python Packages (via `uv add`)

```toml
# Tracing (OpenTelemetry)
opentelemetry-api = "^1.28.0"
opentelemetry-sdk = "^1.28.0"
opentelemetry-instrumentation-django = "^0.49b0"
opentelemetry-instrumentation-psycopg = "^0.49b0"
opentelemetry-instrumentation-redis = "^0.49b0"
opentelemetry-instrumentation-celery = "^0.49b0"
opentelemetry-exporter-otlp = "^1.28.0"

# Metrics (Prometheus)
django-prometheus = "^2.3.1"
prometheus-client = "^0.21.0"

# Profiling (Pyroscope - Flamegraphs!)
pyroscope-io = "^0.8.7"

# Enhanced structlog
python-json-logger = "^2.0.7"  # For Loki compatibility
```

---

## Infrastructure Configuration

### Docker Compose - Development

Add to `docker-compose-base.yml`:

```yaml
  # Observability Stack

  loki:
    image: grafana/loki:3.0.0
    container_name: loki
    restart: unless-stopped
    ports:
      - "3100:3100"
    command: -config.file=/etc/loki/local-config.yaml
    volumes:
      - ./observability/loki-config.yaml:/etc/loki/local-config.yaml
      - loki_data:/loki
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:3100/ready"]
      interval: 10s
      timeout: 5s
      retries: 5

  tempo:
    image: grafana/tempo:2.5.0
    container_name: tempo
    restart: unless-stopped
    ports:
      - "3200:3200"  # Tempo HTTP
      - "4317:4317"  # OTLP gRPC
      - "4318:4318"  # OTLP HTTP
    command: -config.file=/etc/tempo/tempo.yaml
    volumes:
      - ./observability/tempo-config.yaml:/etc/tempo/tempo.yaml
      - tempo_data:/tmp/tempo
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:3200/ready"]
      interval: 10s
      timeout: 5s
      retries: 5

  prometheus:
    image: prom/prometheus:v2.53.0
    container_name: prometheus
    restart: unless-stopped
    ports:
      - "9090:9090"
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--storage.tsdb.retention.time=30d'
    volumes:
      - ./observability/prometheus-config.yml:/etc/prometheus/prometheus.yml
      - prometheus_data:/prometheus
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:9090/-/healthy"]
      interval: 10s
      timeout: 5s
      retries: 5

  pyroscope:
    image: grafana/pyroscope:1.6.0
    container_name: pyroscope
    restart: unless-stopped
    ports:
      - "4040:4040"  # Pyroscope UI
    volumes:
      - pyroscope_data:/var/lib/pyroscope
    environment:
      - PYROSCOPE_LOG_LEVEL=info
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:4040/healthz"]
      interval: 10s
      timeout: 5s
      retries: 5

  grafana:
    image: grafana/grafana:11.1.0
    container_name: grafana
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=admin
      - GF_USERS_ALLOW_SIGN_UP=false
      - GF_INSTALL_PLUGINS=grafana-piechart-panel
    volumes:
      - ./observability/grafana-datasources.yaml:/etc/grafana/provisioning/datasources/datasources.yaml
      - ./observability/grafana-dashboards.yaml:/etc/grafana/provisioning/dashboards/dashboards.yaml
      - ./observability/dashboards:/var/lib/grafana/dashboards
      - grafana_data:/var/lib/grafana
    depends_on:
      loki:
        condition: service_healthy
      tempo:
        condition: service_healthy
      prometheus:
        condition: service_healthy
      pyroscope:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "wget", "--no-verbose", "--tries=1", "--spider", "http://localhost:3000/api/health"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  loki_data:
  tempo_data:
  prometheus_data:
  pyroscope_data:
  grafana_data:
```

**Resource Limits (for `docker-compose-dev.yml`):**
```yaml
  loki:
    mem_limit: 1g
    cpus: 1.0

  tempo:
    mem_limit: 1g
    cpus: 1.0

  prometheus:
    mem_limit: 2g
    cpus: 2.0

  pyroscope:
    mem_limit: 2g
    cpus: 2.0

  grafana:
    mem_limit: 512m
    cpus: 0.5
```

**Total Resource Usage:**
- Memory: ~6.5 GB
- CPU: ~6.5 cores (shared)

### Docker Compose - Staging/Production

Update `/Users/biagio/repos/personal/infra/revel/docker-compose.yaml`:

1. Add observability services (same as dev, adjust resource limits)
2. Configure persistent volumes
3. Set up Grafana with proper authentication (Google SSO)
4. Configure alerting (email/Slack/PagerDuty)

**Production Resource Limits:**
```yaml
  loki:
    mem_limit: 4g
    mem_reservation: 2g
    cpus: 2.0

  tempo:
    mem_limit: 4g
    mem_reservation: 2g
    cpus: 2.0

  prometheus:
    mem_limit: 8g
    mem_reservation: 4g
    cpus: 4.0

  pyroscope:
    mem_limit: 8g
    mem_reservation: 4g
    cpus: 4.0

  grafana:
    mem_limit: 2g
    mem_reservation: 1g
    cpus: 1.0
```

**Total Resource Usage (Production):**
- Memory: ~26 GB (reserved: 13 GB)
- CPU: ~13 cores (shared)
- Acceptable for 32 GB server

---

## Critical Operations to Instrument

### High-Priority Traces (End-to-End)

1. **Ticket Purchase Flow** (`src/events/service/ticket_service.py:checkout`)
   - Span: `ticket.checkout`
     - Span: `ticket.check_eligibility`
       - Span: `eligibility.check_privileged_access`
       - Span: `eligibility.check_invitation`
       - Span: `eligibility.check_membership`
       - Span: `eligibility.check_questionnaire`
     - Span: `ticket.lock_tier` (SELECT FOR UPDATE)
     - Span: `stripe.create_checkout_session` (external API)
     - Span: `payment.create`
     - Span: `ticket.create`

2. **Questionnaire Evaluation** (`src/questionnaires/service.py:evaluate`)
   - Span: `questionnaire.evaluate`
     - Span: `questionnaire.score_multiple_choice`
     - Span: `questionnaire.evaluate_free_text`
       - Span: `llm.api_call` (LLM provider API)
     - Span: `questionnaire.check_fatal_flags`
     - Span: `questionnaire.create_evaluation`
     - Span: `notification.send_evaluation_result`

3. **Event Eligibility Check** (`src/events/models.py:check_eligibility`)
   - Span: `event.check_eligibility`
     - Span: `gate.privileged_access`
     - Span: `gate.event_status`
     - Span: `gate.invitation`
     - Span: `gate.membership`
     - Span: `gate.questionnaire` (recursive check)
     - Span: `gate.ticket_availability`
     - Span: `gate.rsvp_deadline`

4. **Stripe Webhook Processing** (`src/events/service/stripe_service.py:handle_webhook`)
   - Span: `stripe.webhook.handle`
     - Span: `stripe.verify_signature`
     - Span: `payment.update_status`
     - Span: `ticket.activate`
     - Span: `pdf.generate`
     - Span: `ics.generate`
     - Span: `email.send_confirmation`

5. **Event Publication with Notifications** (`src/events/models.py:publish` → `src/events/tasks.py:notify_event_open`)
   - Span: `event.publish`
     - Span: `event.update_status`
     - Span: `task.trigger_notification`
   - Span: `task.notify_event_open` (Celery task)
     - Span: `notification.get_eligible_users` (complex query)
     - Span: `email.render_batch` (for each user)
     - Span: `email.send_batch`

### High-Priority Metrics (Custom Instrumentation)

**Payment Flow:**
- `src/events/service/stripe_service.py:create_checkout_session`
  - Increment: `revel_payment_checkout_sessions_created_total`
  - Observe: `revel_api_request_duration_seconds` (histogram)

- `src/events/service/stripe_service.py:handle_checkout_completed`
  - Increment: `revel_payments_total{status="succeeded"}`
  - Increment: `revel_tickets_sold_total{payment_method="online"}`
  - Increment: `revel_revenue_total` (by amount)

- `src/events/service/stripe_service.py:handle_charge_refunded`
  - Increment: `revel_payments_total{status="refunded"}`
  - Increment: `revel_tickets_cancelled_total{reason="refund"}`

**Questionnaire Flow:**
- `src/questionnaires/service.py:submit`
  - Increment: `revel_questionnaire_submissions_total{status="pending"}`

- `src/questionnaires/service.py:evaluate`
  - Increment: `revel_questionnaire_evaluations_total{result="approved|rejected"}`
  - Observe: `revel_llm_evaluation_duration_seconds`
  - Increment: `revel_llm_tokens_consumed_total`
  - Update: `revel_questionnaire_pass_rate` (gauge)

**Event Lifecycle:**
- `src/events/models.py:save` (on creation)
  - Increment: `revel_events_created_total{has_tickets="true|false"}`

- `src/events/models.py:publish`
  - Increment: `revel_events_active` (gauge)

- `src/events/models.py:cancel`
  - Increment: `revel_events_cancelled_total`
  - Decrement: `revel_events_active` (gauge)

**Email Delivery:**
- `src/events/service/notification_service.py:send_email`
  - Increment: `revel_emails_sent_total{email_type="...", status="success|failure"}`

---

## Implementation Plan

### Phase 1: Foundation (Week 1)
**Goal:** Set up infrastructure and basic instrumentation

1. **Infrastructure Setup**
   - Add observability services to `docker-compose-base.yml`
   - Create configuration files for Loki, Tempo, Prometheus, Pyroscope
   - Set up Grafana with datasource provisioning
   - Add environment variables for enabling/disabling observability
   - Test Docker Compose stack startup

2. **Dependency Installation**
   - Add OpenTelemetry, Prometheus, Pyroscope packages via `uv add`
   - Configure structlog with JSON formatter and PII scrubber
   - Create `src/revel/settings/observability.py` settings module

3. **Basic Structlog Configuration**
   - Configure JSON output for Loki ingestion
   - Add PII scrubbing processor
   - Add timestamp and log level processors
   - Test log output format

### Phase 2: Context Enrichment (Week 1-2)
**Goal:** Automatic context enrichment across all execution contexts

1. **HTTP Request Context**
   - Implement `StructlogContextMiddleware`
   - Add to `MIDDLEWARE` in settings
   - Test request_id propagation
   - Add X-Request-ID response header

2. **Celery Task Context**
   - Add Celery signal handlers for task context
   - Test task_id propagation
   - Verify context isolation between tasks

3. **Django Ninja Integration**
   - Add request/response logging in `UserAwareController`
   - Log endpoint, method, status_code, duration
   - Test with existing API endpoints

### Phase 3: Distributed Tracing (Week 2)
**Goal:** End-to-end request tracing across services

1. **OpenTelemetry Setup**
   - Configure OTLP exporter to Tempo
   - Set up auto-instrumentation for Django, Celery, PostgreSQL, Redis
   - Configure trace sampling (100% dev, 10% prod)
   - Test trace generation and ingestion

2. **Custom Spans for Critical Paths**
   - Add spans to ticket purchase flow
   - Add spans to questionnaire evaluation
   - Add spans to event eligibility checks
   - Add spans to Stripe webhook processing
   - Test trace visualization in Grafana

3. **External Service Tracing**
   - Instrument Stripe API calls
   - Instrument LLM provider API calls
   - Instrument email sending (SMTP)
   - Add error tracking and status codes

### Phase 4: Metrics Collection (Week 2-3)
**Goal:** Business and operational metrics

1. **System Metrics (django-prometheus)**
   - Add django-prometheus middleware
   - Export Django metrics (requests, latency, errors)
   - Export database connection pool metrics
   - Export Celery task metrics
   - Configure Prometheus scraping

2. **Custom Business Metrics**
   - Implement metrics for ticket sales
   - Implement metrics for event lifecycle
   - Implement metrics for questionnaire evaluations
   - Implement metrics for payments
   - Implement metrics for email delivery
   - Test metric ingestion in Prometheus

3. **PostgreSQL Exporter**
   - Add postgres_exporter to Docker Compose
   - Configure scraping in Prometheus
   - Monitor connection pool, query performance, table sizes

4. **Redis Exporter**
   - Add redis_exporter to Docker Compose
   - Configure scraping in Prometheus
   - Monitor memory usage, key count, cache hit rate

### Phase 5: Continuous Profiling (Week 3)
**Goal:** Flamegraphs and performance profiling

1. **Pyroscope Integration**
   - Add `pyroscope-io` SDK to Django startup
   - Configure profiling for web workers (gunicorn)
   - Configure profiling for Celery workers
   - Set profiling sample rate (100 Hz)

2. **Profile Key Operations**
   - Tag profiles with endpoint names
   - Tag profiles with task names
   - Tag profiles with user_id (optional)
   - Test flamegraph generation in Pyroscope UI

3. **Grafana Integration**
   - Add Pyroscope datasource to Grafana
   - Create dashboard for profiling overview
   - Link profiles to traces (profile exemplars)

### Phase 6: Grafana Dashboards (Week 3-4)
**Goal:** Unified observability dashboards

1. **Pre-Built Dashboards**
   - Django overview dashboard (requests, latency, errors)
   - PostgreSQL dashboard (connections, queries, I/O)
   - Redis dashboard (memory, operations, cache hit rate)
   - Celery dashboard (task execution, queue depth, failures)
   - Nginx/Traefik dashboard (if applicable)

2. **Custom Business Dashboards**
   - **Revenue Dashboard**: Ticket sales, revenue by org/event, platform fees
   - **Event Dashboard**: Active events, attendee counts, cancellations
   - **User Dashboard**: Registrations, active users, growth trends
   - **Questionnaire Dashboard**: Submissions, pass/fail rates, evaluation time
   - **Payment Dashboard**: Payment success rate, refunds, Stripe API health
   - **Email Dashboard**: Delivery rate, failures, queue depth

3. **Operational Dashboards**
   - **System Health**: CPU, memory, disk, network per service
   - **Error Tracking**: Error rate by endpoint, error types, stack traces
   - **Performance**: P50/P95/P99 latency, slow queries, flamegraphs
   - **Alerting Overview**: Active alerts, alert history

### Phase 7: Alerting (Week 4)
**Goal:** Proactive issue detection

1. **Critical Alerts**
   - Payment success rate < 95%
   - Stripe webhook processing failures
   - Database connection pool exhaustion
   - High error rate (> 1% of requests)
   - Celery queue backlog > 1000 tasks
   - Disk space < 10%

2. **Warning Alerts**
   - API latency P95 > 2s
   - Email delivery rate < 90%
   - LLM evaluation latency > 10s
   - Redis memory usage > 80%
   - PostgreSQL slow queries (> 5s)

3. **Notification Channels**
   - Email (admin)
   - Slack (if configured)
   - PagerDuty (production only)

### Phase 8: Staging Deployment (Week 4)
**Goal:** Deploy to staging environment

1. **Update Infra Repo**
   - Add observability services to `/Users/biagio/repos/personal/infra/revel/docker-compose.yaml`
   - Configure persistent volumes
   - Set resource limits for 32 GB server
   - Configure Grafana with Google SSO

2. **Staging-Specific Config**
   - 10% trace sampling
   - INFO logs only
   - 30-day retention
   - Alert rules enabled (email only)

3. **Testing**
   - Verify all telemetry data flowing
   - Test dashboards with real traffic
   - Validate alert rules
   - Performance impact assessment

### Phase 9: Production Deployment (Week 5)
**Goal:** Production-ready observability

1. **Production Config**
   - 10% trace sampling (adjustable)
   - INFO logs only
   - 30-day retention
   - All alert rules active
   - PagerDuty integration (if applicable)

2. **Gradual Rollout**
   - Deploy observability infrastructure
   - Enable metrics collection first
   - Enable logging with sampling
   - Enable tracing with low sampling (5%)
   - Gradually increase sampling based on performance impact
   - Enable profiling last

3. **Monitoring the Monitors**
   - Dashboard for observability stack health
   - Alerts for Loki/Tempo/Prometheus/Pyroscope failures
   - Backup and disaster recovery plan

### Phase 10: Documentation (Week 5)
**Goal:** Team enablement

1. **Developer Documentation**
   - How to add custom metrics
   - How to add spans to new code
   - How to use structlog effectively
   - How to interpret flamegraphs
   - How to create custom dashboards

2. **Operational Documentation**
   - Runbook for common alerts
   - How to investigate incidents using traces
   - How to analyze slow queries
   - How to use flamegraphs for performance debugging
   - Backup and restore procedures

3. **Architecture Documentation**
   - Observability stack architecture diagram
   - Data flow diagrams
   - Retention and sampling policies
   - PII scrubbing implementation

---

## Performance Impact Assessment

### Expected Overhead

**Development:**
- Tracing (100% sampling): ~5-10% latency increase
- Metrics collection: ~1-2% CPU overhead
- Profiling: ~1-2% CPU overhead
- Total: ~7-14% overhead (acceptable for dev)

**Production:**
- Tracing (10% sampling): ~0.5-1% latency increase
- Metrics collection: ~1-2% CPU overhead
- Profiling: ~1-2% CPU overhead
- Total: ~2.5-5% overhead (acceptable for prod)

### Mitigation Strategies

1. **Adaptive Sampling**: Increase sampling for errors/slow requests
2. **Async Exports**: Use background threads for OTLP export
3. **Batching**: Batch metric updates to reduce overhead
4. **Selective Instrumentation**: Disable profiling for specific workers if needed

---

## Success Metrics

### Observability Platform
- ✅ All telemetry data (logs, traces, metrics, profiles) flowing to LGTM stack
- ✅ < 5% performance overhead in production
- ✅ 99.9% uptime for observability infrastructure
- ✅ < 1 minute latency for log/trace/metric ingestion

### Developer Experience
- ✅ Developers can trace any request end-to-end
- ✅ Developers can identify performance bottlenecks with flamegraphs
- ✅ Developers can correlate logs, traces, and metrics
- ✅ Mean time to identify root cause (MTTD) < 10 minutes

### Business Value
- ✅ 100% visibility into payment success/failure reasons
- ✅ Proactive alerting catches issues before user reports
- ✅ Data-driven optimization (identify slow endpoints, high-cost operations)
- ✅ Revenue tracking per organization/event in real-time

---

## Open Questions / Future Enhancements

1. **APM Integration**: Consider adding Sentry for error tracking and session replay?
2. **Log Sampling**: Should we sample DEBUG logs even in dev to reduce noise?
3. **Trace Exemplars**: Link metrics to traces for faster debugging?
4. **SLO Tracking**: Define and track Service Level Objectives (e.g., 99.9% payment success)?
5. **Cost Analysis**: Track AWS/infrastructure costs and correlate with usage metrics?
6. **User Journey Tracking**: End-to-end tracking of user flows (signup → event discovery → purchase)?

---

## References

- [Grafana LGTM Stack Documentation](https://grafana.com/docs/)
- [OpenTelemetry Python Documentation](https://opentelemetry.io/docs/instrumentation/python/)
- [Pyroscope Documentation](https://grafana.com/docs/pyroscope/)
- [Structlog Documentation](https://www.structlog.org/)
- [Django Prometheus Exporter](https://github.com/korfuri/django-prometheus)

---

**Document Version:** 1.0
**Last Updated:** 2025-11-04
**Author:** Claude Code (with human oversight)
