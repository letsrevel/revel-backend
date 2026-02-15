# Contributing

We welcome contributions! Whether you're reporting a bug, suggesting a feature, or writing code, here's how to get involved.

## Ways to Contribute

- **Report bugs** -- Check [GitHub Issues](https://github.com/letsrevel/revel-backend/issues), open a new one if it doesn't exist
- **Suggest features** -- Open an issue to start a discussion
- **Write code** -- Pick an existing issue or propose a new one

## Quick Reference

| Topic | What's Covered |
|-------|---------------|
| [Code Style](code-style.md) | Typing conventions, schema patterns, controller patterns, query optimization |
| [Testing](testing.md) | pytest setup, factories, coverage, what to test |
| [Dependency Management](dependency-management.md) | UV workflow, adding/removing packages |
| [CI Pipeline](ci-pipeline.md) | GitHub Actions, `make check`, `make test`, what CI enforces |

## Development Workflow

1. **Fork** the repository on GitHub
2. **Clone** your fork and run `make setup`
3. **Branch** from `main`: `git checkout -b feature/my-feature`
4. **Write code** following the [code style guide](code-style.md)
5. **Write tests** for your changes
6. **Run checks**: `make check && make test`
7. **Push** and open a Pull Request

!!! tip "Commit Messages"
    We use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/): `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`.

## Community

- **Discord**: [Join us](https://discord.gg/zy8nTDqQ) for questions and discussion
- **Issues**: [GitHub Issues](https://github.com/letsrevel/revel-backend/issues) for bugs and features
