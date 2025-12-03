# Observability in CI/CD Testing

## TL;DR

**Observability is DISABLED in CI/CD** to prevent connection errors and speed up tests.

---

## Why Disable Observability in CI?

### The Problem

When `ENABLE_OBSERVABILITY=True` but observability services (Loki, Tempo, Prometheus) aren't running:

```python
# This fails in CI:
LokiHandler tries to connect to http://localhost:3100
→ Connection refused (service doesn't exist)
→ Tests log connection errors
→ QueueListener thread keeps retrying
→ Noise in test output
```

### The Solution

Set `ENABLE_OBSERVABILITY=False` in CI pipeline:

```yaml
# .github/workflows/test.yaml
- name: Disable observability for tests
  run: |
    echo "ENABLE_OBSERVABILITY=False" >> .env
```

---

## What Happens When Disabled?

### Logging Behavior

**With `ENABLE_OBSERVABILITY=True`**:
- ✅ Console logs (JSON format via structlog)
- ✅ Loki logs (async via QueueHandler)
- ✅ Log correlation with traces

**With `ENABLE_OBSERVABILITY=False`**:
- ✅ Console logs only (JSON format via structlog)
- ❌ No Loki handler created
- ❌ No QueueListener started
- ❌ No log shipping attempted

### Tracing Behavior

**With `ENABLE_OBSERVABILITY=True`**:
- ✅ OpenTelemetry instrumentation active
- ✅ Spans exported to Tempo
- ✅ Auto-instrumentation for Django/Celery/PostgreSQL/Redis

**With `ENABLE_OBSERVABILITY=False`**:
- ❌ No tracing initialization
- ❌ No spans created
- ❌ No OTLP exports

### Profiling Behavior

**With `ENABLE_OBSERVABILITY=True`**:
- ℹ️ Profiling skipped (see profiling.py - already disabled)

**With `ENABLE_OBSERVABILITY=False`**:
- ❌ Profiling skipped

---

## Docker Compose Files Used

### CI Pipeline

**File**: `docker-compose-dev.yml`

**Services**:
- ✅ PostgreSQL (for Django ORM)
- ✅ Redis (for Celery)
- ✅ ClamAV (for file scanning tests)
- ❌ Loki (not needed)
- ❌ Tempo (not needed)
- ❌ Prometheus (not needed)
- ❌ Pyroscope (not needed)
- ❌ Alloy (not needed)
- ❌ Grafana (not needed)

**Why minimal?**
- Faster startup (~10 seconds vs ~30 seconds)
- Less resource usage on GitHub Actions
- Only what tests actually require

---

## Code Locations

### Observability Toggle

**File**: `src/revel/settings/observability.py`

```python
ENABLE_OBSERVABILITY = config("ENABLE_OBSERVABILITY", default=True, cast=bool)

if ENABLE_OBSERVABILITY:
    # Create Loki handler
    LOGGING_HANDLERS["loki"] = {...}
    LOGGING_HANDLERS["queue"] = {...}
else:
    # Console only
    pass
```

### CI Configuration

**File**: `.github/workflows/test.yaml`

```yaml
- name: Recreate environment file
  run: cp .env.example .env

- name: Disable observability for tests
  run: |
    echo "ENABLE_OBSERVABILITY=False" >> .env
```

---

## Testing Locally With/Without Observability

### Without Observability (Fast - Like CI)

```bash
# 1. Set env var
echo "ENABLE_OBSERVABILITY=False" >> .env

# 2. Start minimal services
docker compose -f docker-compose-dev.yml up -d

# 3. Run tests
uv run pytest src

# 4. Cleanup
docker compose -f docker-compose-dev.yml down
```

**Expected**: Console logs only, no connection errors

---

### With Observability (Full Stack)

```bash
# 1. Set env var
echo "ENABLE_OBSERVABILITY=True" > .env
cat .env.example >> .env

# 2. Start full stack
docker compose -f docker-compose-dev.yml up -d

# 3. Run tests (logs will go to Loki)
uv run pytest src

# 4. View logs in Grafana
open http://localhost:3000

# 5. Cleanup
docker compose -f docker-compose-dev.yml down
```

**Expected**: Console logs + Loki logs + traces in Tempo

---

## Environment Variable Matrix

| Environment | `ENABLE_OBSERVABILITY` | Docker Compose File | Services Count |
|-------------|----------------------|-------------------|----------------|
| **CI/CD** | `False` (forced) | `docker-compose-dev.yml` | 3 |
| **Local Testing** | `False` (optional) | `docker-compose-dev.yml` | 3 |
| **Local Development** | `True` | `docker-compose-dev.yml` | ~12 |
| **Production** | `True` | `/infra/docker-compose.yaml` | ~15 |

---

## Common Issues & Solutions

### Issue 1: Connection Errors in Tests

**Symptom**:
```
requests.exceptions.ConnectionError:
HTTPConnectionPool(host='localhost', port=3100):
Max retries exceeded with url: /loki/api/v1/push
```

**Cause**: `ENABLE_OBSERVABILITY=True` but Loki not running

**Solution**: Set `ENABLE_OBSERVABILITY=False` in `.env`

---

### Issue 2: No Logs in Grafana During Tests

**Symptom**: Running tests with observability enabled, but no logs appear in Grafana

**Possible Causes**:
1. `ENABLE_OBSERVABILITY=False` (check `.env`)
2. Loki not running (check `docker compose ps`)
3. Wrong `LOKI_URL` (should be `http://localhost:3100`)

**Solution**:
```bash
# Verify observability is enabled
grep ENABLE_OBSERVABILITY .env
# Should show: ENABLE_OBSERVABILITY=True

# Verify Loki is running
docker compose -f docker-compose-dev.yml ps loki
# Should show: running

# Check Loki logs
docker compose -f docker-compose-dev.yml logs loki
```

---

### Issue 3: Tests Pass Locally But Fail in CI

**Symptom**: Tests work on your machine but fail in GitHub Actions

**Possible Causes**:
1. Different `ENABLE_OBSERVABILITY` setting
2. Different Docker Compose file
3. Missing services in CI

**Solution**:
- CI always uses `ENABLE_OBSERVABILITY=False` (automatic)
- CI uses `docker-compose-dev.yml` (minimal)
- Don't rely on observability services in tests

---

## Best Practices

### For Test Code

❌ **DON'T** assume observability is enabled:
```python
# Bad - will fail in CI
def test_something():
    # Assumes Loki is running
    assert loki_has_logs()
```

✅ **DO** make tests observability-agnostic:
```python
# Good - works with or without observability
def test_something():
    # Test business logic, not observability
    assert user.is_active
```

### For Application Code

✅ **DO** check `settings.ENABLE_OBSERVABILITY`:
```python
# Good - graceful degradation
from django.conf import settings

if settings.ENABLE_OBSERVABILITY:
    logger.info("event", trace_id=trace_id)
else:
    logger.info("event")  # No trace correlation
```

✅ **DO** use structlog everywhere:
```python
# Good - works in all environments
import structlog
logger = structlog.get_logger(__name__)
logger.info("user_created", user_id=user.id)
# Outputs JSON to console (always)
# Outputs to Loki (if enabled)
```

### For CI Configuration

✅ **DO** explicitly disable observability:
```yaml
- name: Disable observability for tests
  run: echo "ENABLE_OBSERVABILITY=False" >> .env
```

✅ **DO** use minimal Docker Compose:
```yaml
- name: Spin up docker
  run: docker compose -f docker-compose-dev.yml up -d
```

---

## Performance Comparison

### CI Test Run Time

| Configuration | Services | Startup Time | Test Time | Total |
|--------------|----------|--------------|-----------|-------|
| **Without Observability** (current) | 3 | ~10s | ~60s | ~70s |
| **With Observability** (old) | ~12 | ~30s | ~60s | ~90s |

**Savings**: ~20 seconds per CI run

### Resource Usage

| Configuration | Memory | CPU |
|--------------|--------|-----|
| **Without Observability** | ~2 GB | 2 cores |
| **With Observability** | ~6 GB | 4 cores |

**Savings**: 4 GB RAM, 2 CPU cores

---

## Verification Steps

After making changes to observability configuration, verify:

### 1. Local Tests (No Observability)

```bash
echo "ENABLE_OBSERVABILITY=False" >> .env
docker compose -f docker-compose-dev.yml up -d
uv run pytest src -v
# ✅ Should pass without connection errors
docker compose -f docker-compose-dev.yml down
```

### 2. Local Tests (With Observability)

```bash
echo "ENABLE_OBSERVABILITY=True" > .env
cat .env.example >> .env
docker compose -f docker-compose-dev.yml up -d
uv run pytest src -v
# ✅ Should pass and send logs to Loki
docker compose -f docker-compose-dev.yml down
```

### 3. CI Tests

```bash
# Push to GitHub and check Actions
git push origin your-branch
# ✅ Should pass in GitHub Actions
```

---

## Related Files

- `.github/workflows/test.yaml` - CI pipeline (disables observability)
- `docker-compose-dev.yml` - Minimal services for CI
- `docker-compose-observability.yml` - Full stack with observability
- `src/revel/settings/observability.py` - Observability configuration
- `.env.example` - Template with `ENABLE_OBSERVABILITY=True`

---

## Summary

- ✅ CI/CD **always disables observability** for speed and reliability
- ✅ Local development **can enable/disable** observability as needed
- ✅ Production **always has observability enabled**
- ✅ Tests work the same with or without observability
- ✅ No code changes required - just environment variable toggle

**Key Takeaway**: Observability is an operational feature, not a testing requirement. Tests should validate business logic, not telemetry infrastructure.
