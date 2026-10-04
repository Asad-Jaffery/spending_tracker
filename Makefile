.PHONY: setup test check demo

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install -e '.[dev]'

test:
	.venv/bin/pytest -q

check:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

demo:
	.venv/bin/spend-tracker import examples/synthetic-transactions.json --source demo-checking
