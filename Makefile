# PrimeMover Orchestrator Makefile
#
# Quick Start (full setup from scratch):
#   git clone git@github.com:umatter/primemover-orchestrator.git
#   cd primemover-orchestrator
#   make setup
#
# This will: create venv, install package, clone repos, build runner, start all components

.PHONY: help prereqs setup-server setup setup-quick install venv clean \
        up down restart status doctor \
        db-migrate db-seed db-reset db-init \
        test test-cov lint format typecheck check \
        docker-build docker-run \
        pre-commit hooks \
        compat-check

# Default target
help:
	@echo "PrimeMover Orchestrator"
	@echo ""
	@echo "Setup (run these first):"
	@echo "  make prereqs      - Check system prerequisites (run this first!)"
	@echo "  make setup-server - Full server setup: GUI, VNC, all prerequisites (Ubuntu)"
	@echo "  make setup        - Full setup: venv, install, clone repos, build, start"
	@echo "  make setup-quick  - Quick setup: venv, install only (repos already exist)"
	@echo "  make install      - Install package in development mode"
	@echo "  make venv         - Create Python virtual environment"
	@echo ""
	@echo "Application Lifecycle:"
	@echo "  make up           - Start all components"
	@echo "  make down         - Stop all components"
	@echo "  make restart      - Restart all components"
	@echo "  make status       - Check component status"
	@echo "  make doctor       - Diagnose setup issues"
	@echo ""
	@echo "Database:"
	@echo "  make db-init      - Full database initialization"
	@echo "  make db-migrate   - Run database migrations"
	@echo "  make db-seed      - Seed database with test data"
	@echo "  make db-reset     - Reset database (fresh migrate + seed)"
	@echo ""
	@echo "Development:"
	@echo "  make test         - Run tests"
	@echo "  make test-cov     - Run tests with coverage"
	@echo "  make lint         - Run linter (ruff)"
	@echo "  make format       - Format code (black)"
	@echo "  make typecheck    - Run type checker (mypy)"
	@echo "  make check        - Run all checks (format, lint, typecheck, test)"
	@echo "  make hooks        - Install pre-commit hooks"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build - Build Docker image"
	@echo "  make docker-run   - Run CLI in Docker"
	@echo ""
	@echo "Integration:"
	@echo "  make compat-check - Check for breaking changes in API/Runner repos"
	@echo ""
	@echo "Cleanup:"
	@echo "  make clean        - Remove build artifacts and caches"

# =============================================================================
# Configuration
# =============================================================================

PYTHON := python3
VENV := venv
VENV_BIN := $(VENV)/bin
VENV_PYTHON := $(VENV_BIN)/python
VENV_PIP := $(VENV_BIN)/pip
ORCHESTRATOR := $(VENV_BIN)/orchestrator

# Detect if we're in a venv already
ifdef VIRTUAL_ENV
    PYTHON := python
    VENV_PYTHON := python
    VENV_PIP := pip
    ORCHESTRATOR := orchestrator
endif

# =============================================================================
# Prerequisites Check
# =============================================================================

prereqs:
	@echo "Checking system prerequisites for PrimeMover..."
	@echo ""
	@echo "Required:"
	@printf "  %-20s" "git"; command -v git >/dev/null 2>&1 && echo "✓ $$(git --version)" || echo "✗ NOT INSTALLED"
	@printf "  %-20s" "docker"; command -v docker >/dev/null 2>&1 && echo "✓ $$(docker --version 2>/dev/null | head -1)" || echo "✗ NOT INSTALLED"
	@printf "  %-20s" "docker-compose"; (command -v docker-compose >/dev/null 2>&1 || docker compose version >/dev/null 2>&1) && echo "✓ installed" || echo "✗ NOT INSTALLED"
	@printf "  %-20s" "python3"; command -v python3 >/dev/null 2>&1 && echo "✓ $$(python3 --version)" || echo "✗ NOT INSTALLED"
	@printf "  %-20s" "java"; command -v java >/dev/null 2>&1 && echo "✓ $$(java -version 2>&1 | head -1)" || echo "✗ NOT INSTALLED"
	@printf "  %-20s" "mvn"; command -v mvn >/dev/null 2>&1 && echo "✓ $$(mvn --version 2>/dev/null | head -1)" || echo "✗ NOT INSTALLED"
	@echo ""
	@echo "Optional (for GUI):"
	@printf "  %-20s" "node"; command -v node >/dev/null 2>&1 && echo "✓ $$(node --version)" || echo "- not installed"
	@printf "  %-20s" "npm"; command -v npm >/dev/null 2>&1 && echo "✓ $$(npm --version)" || echo "- not installed"
	@echo ""
	@echo "Optional (for browser automation):"
	@printf "  %-20s" "chrome"; (command -v google-chrome >/dev/null 2>&1 || command -v chromium >/dev/null 2>&1) && echo "✓ installed" || echo "- not installed"
	@echo ""
	@echo "Docker daemon:"
	@docker info >/dev/null 2>&1 && echo "  ✓ Docker is running" || echo "  ✗ Docker is NOT running (start it or check permissions)"
	@echo ""
	@echo "---"
	@echo "Install missing prerequisites before running 'make setup'"
	@echo "See README.md for installation commands"

# Full server setup (Ubuntu): installs GUI, VNC, all prerequisites
setup-server:
	@echo "Running server setup script..."
	@echo "This will install GUI desktop, VNC server, and all prerequisites."
	@echo ""
	@bash scripts/setup-server.sh

# =============================================================================
# Setup Targets
# =============================================================================

# Full setup: creates venv, installs package, clones repos, builds runner, starts
setup: venv install
	@echo ""
	@echo "Running orchestrator setup..."
	@echo "This will clone repositories, build the runner, and start all components."
	@echo ""
	$(ORCHESTRATOR) setup

# Quick setup: for when repos already exist
setup-quick: venv install
	@echo ""
	@echo "Running orchestrator setup (skip clone)..."
	$(ORCHESTRATOR) setup --skip-clone

# Create virtual environment
venv:
	@if [ ! -d "$(VENV)" ]; then \
		echo "Creating virtual environment..."; \
		$(PYTHON) -m venv $(VENV); \
		echo "Virtual environment created at $(VENV)/"; \
	else \
		echo "Virtual environment already exists at $(VENV)/"; \
	fi

# Install package in development mode
install: venv
	@echo "Installing package in development mode..."
	$(VENV_PIP) install --upgrade pip
	$(VENV_PIP) install -e ".[dev]"
	@echo ""
	@echo "Installation complete. Activate venv with: source $(VENV)/bin/activate"

# =============================================================================
# Application Lifecycle
# =============================================================================

up:
	$(ORCHESTRATOR) up

down:
	$(ORCHESTRATOR) down

restart:
	$(ORCHESTRATOR) restart

status:
	$(ORCHESTRATOR) status -v

doctor:
	$(ORCHESTRATOR) doctor

# =============================================================================
# Database
# =============================================================================

db-init:
	$(ORCHESTRATOR) db init

db-migrate:
	$(ORCHESTRATOR) db migrate

db-seed:
	$(ORCHESTRATOR) db seed

db-reset:
	$(ORCHESTRATOR) db reset

# =============================================================================
# Development
# =============================================================================

test:
	$(VENV_BIN)/pytest

test-cov:
	$(VENV_BIN)/pytest --cov=orchestrator --cov=experiment_designer --cov-report=term-missing

lint:
	$(VENV_BIN)/ruff check orchestrator/ experiment_designer/

format:
	$(VENV_BIN)/black orchestrator/ experiment_designer/
	$(VENV_BIN)/ruff check orchestrator/ experiment_designer/ --fix

typecheck:
	$(VENV_BIN)/mypy orchestrator/ experiment_designer/ --ignore-missing-imports

# Run all checks (useful before committing)
check: format lint typecheck test
	@echo ""
	@echo "All checks passed!"

# Install pre-commit hooks
hooks:
	$(VENV_BIN)/pre-commit install
	@echo "Pre-commit hooks installed."

# Run pre-commit on all files
pre-commit:
	$(VENV_BIN)/pre-commit run --all-files

# =============================================================================
# Docker
# =============================================================================

docker-build:
	docker build -t primemover-orchestrator .

docker-run:
	docker run --rm -it primemover-orchestrator $(ARGS)

# =============================================================================
# Integration Compatibility
# =============================================================================

# Check for breaking changes in integrated repositories
compat-check:
	@$(VENV_PYTHON) scripts/check_compatibility.py --days $(or $(DAYS),7)

# =============================================================================
# Cleanup
# =============================================================================

clean:
	rm -rf build/
	rm -rf dist/
	rm -rf *.egg-info/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf .ruff_cache/
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@echo "Cleaned build artifacts and caches."

# Clean everything including venv
clean-all: clean
	rm -rf $(VENV)/
	@echo "Removed virtual environment."
