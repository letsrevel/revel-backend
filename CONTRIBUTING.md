# Contributing to Revel

Thank you for your interest in contributing to Revel! We're excited to have you join our community. Whether you're reporting a bug, suggesting a new feature, or writing code, your contributions are valuable.

This document provides guidelines for contributing to the project to ensure a smooth and effective process for everyone.

## How to Contribute

There are several ways you can contribute to the project:

*   **Reporting Bugs:** If you find a bug, please check our [GitHub Issues](https://github.com/biagiodistefano/revel/issues) to see if it has already been reported. If not, please open a new issue.
*   **Suggesting Enhancements:** If you have an idea for a new feature or an improvement to an existing one, open an issue to start a discussion.
*   **Writing Code:** If you're ready to contribute code, you can pick an existing issue or propose a new one.

## Setting Up Your Development Environment

Our goal is to make setup as easy as possible. You'll need `make`, `Docker`, and Python 3.12+.

1.  **Fork** the repository on GitHub.
2.  **Clone** your fork locally:
    ```bash
    git clone https://github.com/your-username/revel.git
    cd revel
    ```
3.  **Run the setup command:**
    ```bash
    make setup
    ```
    This single command handles everything: creating a virtual environment, installing dependencies, setting up Docker services, and running the server.

## Development Workflow

1.  **Create a branch** for your feature or bugfix from the `main` branch:
    ```bash
    git checkout -b feature/my-new-feature
    ```
2.  **Write your code.** Follow the project's coding style and structure (see below).
3.  **Write tests** for your changes. We use `pytest` and aim for high test coverage. All new features must be accompanied by tests.
4.  **Run all checks** to ensure your code is clean, formatted, and passes all tests:
    ```bash
    make check
    make test
    ```
    These commands must pass before you submit your changes.
5.  **Commit your changes.** We recommend using [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for clear and descriptive commit messages (e.g., `feat: Add RSVP deadline to events`).
6.  **Push your branch** to your fork:
    ```bash
    git push origin feature/my-new-feature
    ```
7.  **Open a Pull Request** on the main Revel repository. Provide a clear description of your changes and link to any relevant issues.

## Coding Style and Conventions

Consistency is key. We follow these standards to maintain a clean and readable codebase.

*   **Style Guide:** [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html).
*   **Service Layer:** We use a hybrid approach - function-based services for stateless operations (CRUD, queries), class-based for stateful workflows (checkout, eligibility). See `CLAUDE.md` for detailed patterns.
*   **Formatting & Linting:** We use `ruff` for both. Run `make format` to automatically format your code before committing. `make lint` will check for style issues.
*   **Type Hinting:** All new code must be fully type-hinted using Python 3.12+ syntax. We use `mypy` for static analysis. Run `make mypy` to check your types.
    *   Always use `import typing as t` (never `from typing import ...`).
    *   Do not use `# type: ignore[no-untyped-def]`. All function signatures must be typed.
    *   Use string forward references for type hints where necessary (e.g., `-> 'MyModel'`).
*   **Docstrings:** Use Google-style docstrings for all public modules, classes, functions, and methods.
*   **Internationalization (i18n):** All user-facing strings must be wrapped with Django's translation utilities. See [i18n.md](i18n.md) for detailed guidelines on adding translatable strings and supporting new languages.

## Issue Tracking

We use GitHub Issues to track bugs and feature requests. You can create a new issue using the GitHub CLI:

```bash
gh issue create --title 'Your Issue Title' --body $'A detailed description of the issue.\n\nUse newlines for clarity.' --assignee @me
```

Please add relevant labels (bug, enhancement, user story) to your issue.

## Final Word
Clean code is the most important principle of this project. By following these guidelines, you help us keep the codebase maintainable, scalable, and a pleasure to work on. Thank you for your contribution!
