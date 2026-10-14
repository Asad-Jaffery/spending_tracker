.PHONY: setup test check demo run

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install -e '.[dev]'
	npm install

test:
	.venv/bin/pytest -q

check:
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .
	npm run build

demo:
	.venv/bin/spend-tracker demo

run:
	npm run dev
