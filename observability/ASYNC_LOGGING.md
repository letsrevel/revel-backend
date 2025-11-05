# Async Logging Implementation

## Overview

Revel uses **asynchronous logging** to prevent Loki HTTP requests from blocking request processing. This ensures high performance even with high log volumes or Loki network latency.

## Architecture

```
┌─────────────────┐
│  Django Request │
│     Thread      │
└────────┬────────┘
         │
         │ log.info("...")  ← Non-blocking!
         ↓
┌─────────────────┐
│  QueueHandler   │  ← Instantly puts log in queue
│   (In-Memory)   │     and returns
└────────┬────────┘
         │
         │ Queue (max 10,000 entries)
         ↓
┌─────────────────┐
│ QueueListener   │  ← Background thread
│ (Background     │     processes queue
│  Thread)        │
└────────┬────────┘
         │
         │ HTTP POST to Loki
         ↓
┌─────────────────┐
│   Loki Server   │
└─────────────────┘
```

## Key Benefits

1. **Non-Blocking**: Log statements return instantly (microseconds)
2. **Resilient**: Loki failures don't slow down or crash the app
3. **Bounded Memory**: Queue size limit (10,000) prevents memory exhaustion
4. **Graceful Degradation**: If queue fills, oldest logs are dropped (app stays responsive)

## Implementation Details

### Queue Configuration

Location: `src/revel/settings/observability.py:165-171`

```python
LOGGING_HANDLERS["queue"] = {
    "class": "logging.handlers.QueueHandler",
    "queue": {
        "()": "queue.Queue",
        "maxsize": 10000,  # Drop logs if queue fills
    },
}
```

### QueueListener Startup

Location: `src/common/apps.py:24-82`

The `QueueListener` is started in `CommonConfig.ready()`:
- Runs in a background daemon thread
- Automatically started when Django boots
- Processes logs from queue and sends to Loki

### Noisy Logger Filtering

To reduce log volume, verbose libraries are configured to only log warnings/errors:

```python
"silk.middleware": {"level": "WARNING"},      # Silk is very noisy
"silk.model_factory": {"level": "ERROR"},     # Even noisier
"asyncio": {"level": "WARNING"},
"multipart": {"level": "WARNING"},
"urllib3": {"level": "WARNING"},
"django.db.backends": {"level": "WARNING"},   # Only slow queries/errors
```

## Performance Impact

### Before (Synchronous Logging)
- Log statement: **10-50ms** (network I/O to Loki)
- 100 logs/request = **1-5 seconds** of blocking time
- Loki downtime = **app crashes or timeouts**

### After (Async Logging)
- Log statement: **<1ms** (queue append)
- 100 logs/request = **<100ms** total
- Loki downtime = **no impact on app** (logs queued)

**~50-100x faster** for high log volumes.

## Monitoring

### Check Queue Status

```python
import logging.handlers
from django.apps import apps

common_app = apps.get_app_config("common")
if common_app.queue_listener:
    # Get queue size
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.QueueHandler):
            queue_size = handler.queue.qsize()
            queue_maxsize = handler.queue.maxsize
            print(f"Queue: {queue_size}/{queue_maxsize}")
```

### Warning Signs

1. **Queue near capacity** (>8,000/10,000):
   - Increase `maxsize` or reduce log volume
   - Add log sampling (see below)

2. **Logs missing in Loki**:
   - Check `docker logs loki` for errors
   - Verify network connectivity
   - Queue may be dropping logs (too much volume)

## Future Optimizations

If log volume is still too high, consider:

### 1. Log Sampling

Only send 10% of INFO logs to Loki (always send WARNING+):

```python
class SamplingFilter(logging.Filter):
    def __init__(self, sample_rate=0.1):
        self.sample_rate = sample_rate

    def filter(self, record):
        if record.levelno >= logging.WARNING:
            return True
        return random.random() < self.sample_rate

# Add to Loki handler
LOGGING_HANDLERS["loki"]["filters"] = ["sampling"]
```

### 2. Promtail (Production Best Practice)

Replace in-app Loki handler with Promtail:
- App logs only to stdout (zero overhead)
- Promtail scrapes logs and sends to Loki
- Complete decoupling

See: https://grafana.com/docs/loki/latest/send-data/promtail/

## Testing

### Generate Load

```python
import logging
logger = logging.getLogger(__name__)

for i in range(1000):
    logger.info(f"Test log {i}")
```

### Verify Non-Blocking

```python
import time
import logging

logger = logging.getLogger(__name__)

start = time.time()
for i in range(100):
    logger.info(f"Log {i}")
elapsed = time.time() - start

print(f"100 logs took {elapsed*1000:.2f}ms")
# Should be <100ms (async)
# Would be >1000ms (sync)
```

## Troubleshooting

### Logs not appearing in Loki

1. Check QueueListener started:
   ```bash
   # Look for this in Django startup logs:
   # "Started QueueListener for async Loki logging"
   ```

2. Check Loki connectivity:
   ```bash
   docker logs loki | grep error
   curl http://localhost:3100/ready
   ```

3. Check queue handler:
   ```python
   import logging
   logger = logging.getLogger()
   print([type(h).__name__ for h in logger.handlers])
   # Should include: ['StreamHandler', 'QueueHandler']
   ```

### Queue filling up

Reduce log volume:
1. Increase noisy logger levels (WARNING → ERROR)
2. Add log sampling
3. Reduce DEBUG logging in production

### Memory usage

Queue max memory: `10,000 logs × ~1KB/log = ~10MB`

If memory is a concern, reduce `maxsize` to 5,000 or 2,000.

## References

- [Python QueueHandler docs](https://docs.python.org/3/library/logging.handlers.html#queuehandler)
- [Loki best practices](https://grafana.com/docs/loki/latest/best-practices/)
- [Django logging configuration](https://docs.djangoproject.com/en/5.1/topics/logging/)
