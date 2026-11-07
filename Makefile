.PHONY: install test lint run demo bench ci build docker clean

install:
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check src tests scripts
	mypy src/engram

run:
	uvicorn --factory engram.api:get_app --reload --host 127.0.0.1 --port 8000

demo:
	python -m engram.demo_agent

bench:
	python -m engram.cli bench --out reports/benchmark.md --html reports/benchmark.html

ci: lint test

build:
	uv build --out-dir dist

docker:
	docker compose up --build

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist
	find . -name "__pycache__" -type d -prune -exec rm -rf {} +
