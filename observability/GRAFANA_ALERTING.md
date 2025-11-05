# Grafana Alerting for Revel

## Overview

Revel uses **Grafana Alerting** as the primary notification system for errors, performance issues, and business-critical events. All exceptions and important events are logged to **Loki** via structured logging, and Grafana monitors these logs to trigger alerts.

This approach provides:
- ✅ **Centralized alerting** - One place for all alerts
- ✅ **Powerful querying** - LogQL for complex conditions
- ✅ **Flexible routing** - Email, Slack, PagerDuty, Discord, webhooks
- ✅ **Trace/log correlation** - Jump from alert → trace → logs
- ✅ **No DB overhead** - No writes to PostgreSQL for errors
- ✅ **Industry standard** - Battle-tested Grafana Alertmanager

## Architecture

```
Application (Django)
  → Structured Logs (structlog)
    → Loki (via QueueHandler)
      → Grafana Alert Rules (LogQL queries)
        → Alertmanager (routing/grouping)
          → Notification Channels (Email/Slack/etc)
```

## Alert Rule Examples

### 1. High Error Rate

Alert when unhandled exceptions exceed a threshold.

```yaml
Rule Name: HighErrorRate
Query: rate({service="revel", level="error"} |= "unhandled_exception" [5m]) > 0.1
For: 2m
Severity: critical
Labels:
  team: backend
  component: api
Annotations:
  summary: "High error rate detected: {{ $value }} errors/sec"
  description: "Unhandled exceptions are occurring at {{ $value }} per second over the last 5 minutes"
```

**When to use**: Catch sudden spikes in application errors.

---

### 2. Stripe Payment Failures

Alert on payment processing failures.

```yaml
Rule Name: StripePaymentFailures
Query: |
  sum(count_over_time(
    {service="revel"}
      |~ "stripe_payment_failed|stripe_refund_.*|stripe_session_unresolved_payment"
    [10m]
  )) > 5
For: 5m
Severity: high
Labels:
  team: payments
  component: stripe
Annotations:
  summary: "Multiple Stripe payment failures detected"
  description: "{{ $value }} payment-related failures in the last 10 minutes"
  runbook_url: "https://wiki.example.com/runbooks/stripe-failures"
```

**When to use**: Critical for revenue - immediate response needed.

---

### 3. Authentication Failures

Alert on suspicious authentication activity.

```yaml
Rule Name: HighAuthFailureRate
Query: |
  sum(rate(
    {service="revel"}
      |~ "otp_verification_failed|password_reset_.*_failed|google_token_verification_failed"
    [5m]
  )) > 1
For: 3m
Severity: warning
Labels:
  team: security
  component: auth
Annotations:
  summary: "High authentication failure rate"
  description: "{{ $value }} auth failures/sec - potential brute force attack"
```

**When to use**: Security monitoring - may indicate attack or misconfiguration.

---

### 4. GDPR Export Failures

Alert when data export requests fail.

```yaml
Rule Name: GDPRExportFailures
Query: |
  count_over_time(
    {service="revel"} |= "gdpr_export_failed"
    [30m]
  ) > 0
For: 0m
Severity: high
Labels:
  team: compliance
  component: gdpr
Annotations:
  summary: "GDPR data export failed"
  description: "User data export request failed - compliance risk"
  runbook_url: "https://wiki.example.com/runbooks/gdpr-failures"
```

**When to use**: Compliance-critical - must not fail unnoticed.

---

### 5. LLM Evaluation Failures

Alert when questionnaire evaluation fails.

```yaml
Rule Name: QuestionnaireEvaluationFailures
Query: |
  sum(count_over_time(
    {service="revel"} |= "questionnaire_evaluation_task_failed"
    [15m]
  )) > 3
For: 5m
Severity: warning
Labels:
  team: backend
  component: questionnaires
Annotations:
  summary: "Questionnaire evaluation failures detected"
  description: "{{ $value }} evaluation failures - check LLM API status"
```

**When to use**: Business-critical feature - affects user onboarding.

---

### 6. High LLM Token Usage

Alert on unexpectedly high token consumption (cost monitoring).

```yaml
Rule Name: HighLLMTokenUsage
Query: |
  sum(sum_over_time(
    {service="revel"}
      |= "questionnaire_llm_evaluation_completed"
      | json
      | unwrap tokens_used [1h]
  )) > 1000000
For: 0m
Severity: warning
Labels:
  team: backend
  component: cost
Annotations:
  summary: "High LLM token usage detected"
  description: "{{ $value }} tokens used in the last hour - check for abuse or bugs"
```

**When to use**: Cost control - prevent runaway LLM spending.

---

### 7. Database Connection Issues

Alert on database errors (though rare with Django's connection pooling).

```yaml
Rule Name: DatabaseErrors
Query: |
  sum(count_over_time(
    {service="revel", level="error"}
      |~ "(?i)(database|connection|postgres|psycopg)"
    [5m]
  )) > 10
For: 2m
Severity: critical
Labels:
  team: infrastructure
  component: database
Annotations:
  summary: "Database errors detected"
  description: "{{ $value }} database-related errors in the last 5 minutes"
```

**When to use**: Critical infrastructure monitoring.

---

### 8. Celery Task Failures

Alert on background task failures.

```yaml
Rule Name: CeleryTaskFailures
Query: |
  sum(rate(
    {service="revel"}
      |~ ".*_task_failed"
    [10m]
  )) > 0.5
For: 5m
Severity: warning
Labels:
  team: backend
  component: celery
Annotations:
  summary: "High Celery task failure rate"
  description: "{{ $value }} task failures/sec - check worker health"
```

**When to use**: Background job monitoring - emails, notifications, etc.

---

### 9. Missing Mandatory Questions

Alert when users submit questionnaires with missing mandatory fields.

```yaml
Rule Name: MissingMandatoryQuestions
Query: |
  sum(count_over_time(
    {service="revel"} |= "questionnaire_missing_mandatory_questions"
    [1h]
  )) > 10
For: 0m
Severity: info
Labels:
  team: product
  component: questionnaires
Annotations:
  summary: "Users submitting incomplete questionnaires"
  description: "{{ $value }} submissions with missing mandatory questions - UX issue?"
```

**When to use**: Product analytics - may indicate UX problems.

---

### 10. Account Deletions Blocked

Alert when users try to delete accounts but are blocked by owned organizations.

```yaml
Rule Name: BlockedAccountDeletions
Query: |
  sum(count_over_time(
    {service="revel"} |= "account_deletion_blocked_owns_organizations"
    [24h]
  )) > 5
For: 0m
Severity: info
Labels:
  team: support
  component: accounts
Annotations:
  summary: "Multiple users blocked from deleting accounts"
  description: "{{ $value }} users unable to delete accounts due to org ownership"
```

**When to use**: Support ticket prevention - proactive outreach.

---

## Notification Channels

### Email

**Configuration** (Grafana UI):
1. Go to **Alerting** → **Contact points**
2. Click **New contact point**
3. Select **Email**
4. Configure SMTP settings:
   ```
   From: alerts@letsrevel.io
   To: devops@letsrevel.io, team@letsrevel.io
   Subject: [Revel Alert] {{ .CommonLabels.alertname }}
   ```

### Slack

**Configuration**:
1. Create Slack webhook: https://api.slack.com/messaging/webhooks
2. Add contact point with webhook URL
3. Example message template:
   ```json
   {
     "text": "🚨 *{{ .CommonLabels.alertname }}*",
     "blocks": [
       {
         "type": "section",
         "text": {
           "type": "mrkdwn",
           "text": "*Summary:* {{ .CommonAnnotations.summary }}\n*Description:* {{ .CommonAnnotations.description }}"
         }
       }
     ]
   }
   ```

### Discord

**Configuration**:
1. Create Discord webhook in channel settings
2. Add contact point with webhook URL
3. Use similar JSON template as Slack

### PagerDuty

**Configuration**:
1. Get PagerDuty integration key
2. Add contact point → PagerDuty
3. Configure severity mapping:
   - `critical` → P1
   - `high` → P2
   - `warning` → P3

---

## Notification Policies

Route alerts to different channels based on severity and team.

**Example routing tree**:

```yaml
Root Policy:
  - Receiver: default-email
  - Group by: [alertname, cluster]
  - Group wait: 30s
  - Group interval: 5m
  - Repeat interval: 4h

  Child Policies:
    - Match: severity=critical
      Receiver: pagerduty-oncall
      Continue: true

    - Match: team=payments
      Receiver: slack-payments-channel
      Continue: true

    - Match: team=security
      Receiver: slack-security-channel
      Continue: false

    - Match: severity=info
      Receiver: slack-analytics-channel
      Repeat interval: 24h
```

**Explanation**:
- **Critical alerts** → PagerDuty + email (wake up on-call)
- **Payment alerts** → Slack #payments + email
- **Security alerts** → Slack #security only (no spam)
- **Info alerts** → Slack #analytics (daily digest)

---

## Debugging with Grafana Explore

### Find Recent Errors

```logql
{service="revel", level="error"}
  | json
  | line_format "{{.method}} {{.path}} - {{.event}} - {{.exception_type}}"
```

### Errors by User

```logql
{service="revel", level="error"}
  | json
  | user_id != ""
  | line_format "User {{.user_id}}: {{.event}}"
```

### Payment Failures with Amount

```logql
{service="revel"}
  |= "stripe_payment_failed"
  | json
  | line_format "Failed payment: {{.amount}} - {{.payment_id}}"
```

### Top Error Paths

```logql
topk(10,
  sum by (path) (
    count_over_time({service="revel", level="error"}[24h])
  )
)
```

### Correlation: Trace → Logs

When you have a trace ID from Tempo:
```logql
{service="revel"} |= "trace_id=<YOUR_TRACE_ID>"
```

---

## Best Practices

### 1. Alert on Symptoms, Not Causes

❌ **Bad**: Alert on "high CPU usage"
✅ **Good**: Alert on "slow response times" or "error rate increase"

### 2. Avoid Alert Fatigue

- Use `For` duration to filter transient issues
- Group related alerts (e.g., all payment alerts together)
- Set appropriate repeat intervals (don't spam every 5 minutes)

### 3. Include Runbooks

Always add `runbook_url` annotation with debugging steps:

```yaml
Annotations:
  summary: "Database connection pool exhausted"
  description: "All connections in use - potential leak or traffic spike"
  runbook_url: "https://wiki.example.com/runbooks/db-connection-pool"
```

### 4. Use Severity Wisely

- **critical**: Wakes people up at 3am (money loss, data breach, outage)
- **high**: Needs immediate attention during work hours (payment failures)
- **warning**: Should be investigated today (auth failures, task failures)
- **info**: For tracking/analytics (user behavior patterns)

### 5. Test Alerts

```bash
# Send test notification from Grafana UI
Alerting → Contact points → <Your contact> → Test

# Manually trigger logs matching alert
# (e.g., force an error in dev environment)
```

### 6. Alert on Aggregates, Not Individual Events

❌ **Bad**: Alert on every single error
✅ **Good**: Alert when error rate exceeds threshold

### 7. Silence During Maintenance

Use **Silences** in Grafana Alerting during:
- Planned deployments
- Database migrations
- Load testing

---

## Migration from DB Error Tracking

### What Changed

**Before**:
```python
# Exception → DB write → Admin UI → Manual email
track_internal_error.delay(...)  # Celery task writes to DB
```

**After**:
```python
# Exception → Loki → Grafana Alert → Auto notification
logger.error("unhandled_exception", ...)  # Structured log
```

### Benefits

1. **No DB overhead**: Errors don't write to PostgreSQL
2. **Better querying**: LogQL > SQL for log analysis
3. **Auto notifications**: No manual email sending code
4. **Correlation**: Link errors to traces and metrics
5. **Scalability**: Loki handles millions of logs/sec

### Cleanup (Future)

After verifying Grafana alerting works:

1. **Stop writing** to Error models (already done ✅)
2. **Configure alerts** in Grafana (documented above)
3. **Validate** for 1-2 weeks
4. **Remove** Error and ErrorOccurrence models
5. **Remove** `src/api/tasks.py::track_internal_error`
6. **Remove** cleanup task in Celery beat

---

## Example Alert Workflow

**Scenario**: Payment failure spike

1. **Alert fires** (HighStripeFailureRate)
2. **Notification** sent to Slack #payments + PagerDuty
3. **Engineer** opens Grafana from alert link
4. **Debug** in Explore:
   ```logql
   {service="revel"} |= "stripe_payment_failed" | json
   ```
5. **Correlate** to traces (find slow payment API calls)
6. **Fix** issue (e.g., Stripe API timeout)
7. **Silence** alert during fix deployment
8. **Verify** alert clears after fix

---

## Further Reading

- [Grafana Alerting Docs](https://grafana.com/docs/grafana/latest/alerting/)
- [LogQL Query Language](https://grafana.com/docs/loki/latest/logql/)
- [Alertmanager Routing](https://grafana.com/docs/grafana/latest/alerting/manage-notifications/create-notification-policy/)
- [Best Practices for Alerting](https://docs.google.com/document/d/199PqyG3UsyXlwieHaqbGiWVa8eMWi8zzAn0YfcApr8Q/edit) (Google SRE)

---

## Quick Start Checklist

- [ ] Access Grafana at http://localhost:3000 (or production URL)
- [ ] Configure SMTP for email notifications
- [ ] Set up Slack/Discord webhooks (optional)
- [ ] Create alert rules from examples above
- [ ] Set up notification policies (routing)
- [ ] Test alerts with dummy errors
- [ ] Document runbooks for each alert
- [ ] Train team on responding to alerts
- [ ] Set up silences for maintenance windows
- [ ] Monitor alert effectiveness (false positives?)
