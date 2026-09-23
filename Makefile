.PHONY: setup run test check migrate

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install -e '.[dev]'
	.venv/bin/alembic upgrade head

run:
	.venv/bin/uvicorn spend_tracker.main:app --reload --host 127.0.0.1

migrate:
	.venv/bin/alembic upgrade head

test:
	.venv/bin/pytest -q

check:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
