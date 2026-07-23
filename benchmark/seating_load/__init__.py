"""Seating engine load-test scenarios (hold storm, best-available herd, polling, purchase race).

Run against a live ``make run-e2e`` server:

    uv run python -m benchmark.seating_load --scenario all

See ``__main__.py`` for the CLI and ``scenarios.py`` for what each scenario asserts.
"""
