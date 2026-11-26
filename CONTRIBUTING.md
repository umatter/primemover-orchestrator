# Contributing to PrimeMover Orchestrator

Thank you for your interest in contributing to the PrimeMover Orchestrator!

## Development Setup

```bash
# Clone the repository
git clone https://github.com/umatter/primemover-orchestrator.git
cd primemover-orchestrator

# Create virtual environment
python -m venv venv
source venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"

# Set up pre-commit hooks
pre-commit install
```

## Code Quality

We use automated tools to maintain code quality. Pre-commit hooks will run automatically on each commit.

### Formatting

```bash
# Format code with Black
black orchestrator/ pmspec/
```

### Linting

```bash
# Lint with Ruff (auto-fix)
ruff check orchestrator/ pmspec/ --fix
```

### Type Checking

```bash
# Type check with mypy
mypy orchestrator/ pmspec/ --ignore-missing-imports
```

## Testing

```bash
# Run all tests
pytest

# Run specific test file
pytest orchestrator/tests/test_cli.py -v

# Run with coverage
pytest --cov=orchestrator --cov=pmspec

# Run tests matching a pattern
pytest -k "test_status" -v
```

### Writing Tests

- Place tests in `orchestrator/tests/` or `pmspec/tests/`
- Use pytest fixtures from `conftest.py` for common setup
- Mock external services (API, Loki, CloudWatch) in tests
- Aim for clear, focused test cases

## Project Structure

```
orchestrator/           # Main CLI and monitoring tools
├── cli.py             # Click CLI commands
├── components/        # Component lifecycle managers
├── clients/           # External service clients
├── logs/              # Log parsing and aggregation
├── tracing/           # Correlation ID and tracing
└── tests/             # Tests

pmspec/   # YAML experiment DSL
├── cli.py            # pmspec CLI
├── spec/             # Spec parsing and conversion
└── tests/            # Tests
```

## Adding Features

### New CLI Command

1. Add command function in `orchestrator/cli.py`
2. Use Click decorators for options
3. Import modules lazily to improve CLI startup time
4. Add tests in `orchestrator/tests/test_cli.py`

```python
@cli.command()
@click.option("-t", "--time-range", default="1h")
@click.pass_context
def my_command(ctx: click.Context, time_range: str) -> None:
    """Command description."""
    from orchestrator.some_module import SomeClass
    # Implementation
```

### New MCP Tool

1. Add function in `orchestrator/mcp_server/server.py`
2. Use `@mcp.tool()` decorator
3. Return JSON-serialized results

### New Log Parser

1. Create parser in `orchestrator/logs/parsers/`
2. Return list of `LogEntry` objects
3. Register in `LogAggregator._query_log_files()`

## Commit Messages

Use clear, descriptive commit messages:

```
Add crawler tracing command

- Implement trace crawler <id> command
- Add tests for crawler tracing
- Update CLI help text
```

## Pull Requests

1. Create a feature branch from `master`
2. Make your changes with tests
3. Ensure all tests pass: `pytest`
4. Ensure code quality: `pre-commit run --all-files`
5. Submit PR with clear description

## Documentation

- Update `CLAUDE.md` for significant changes (it guides AI assistants)
- Update `README.md` for user-facing features
- Add docstrings to public functions
- Update `docs/` for detailed feature documentation

## Questions?

Open an issue on GitHub for questions or discussions.
