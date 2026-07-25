"""HTTP-level load tests and benchmarks run against a live local server.

Unlike ``src/events/management/commands/benchmark`` (which drives services
in-process), everything in this package exercises the real HTTP stack:
gunicorn -> Django -> PgBouncer -> Postgres.
"""
