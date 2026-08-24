.PHONY: install lint fmt typecheck test test-unit test-integration test-e2e \
        up down logs ps migrate api worker ui

install:
	pip install -e ".[dev]"

lint:
	ruff check app tests

fmt:
	ruff check --fix app tests

typecheck:
	mypy app

test:
	pytest

test-unit:
	pytest -m unit

test-integration:
	pytest -m integration

test-e2e:
	pytest -m e2e

up:
	docker compose up --build -d

down:
	docker compose down

logs:
	docker compose logs -f api worker

ps:
	docker compose ps

migrate:
	docker compose exec api alembic upgrade head

api:
	uvicorn app.presentation.api.main:app --reload --port 8000

worker:
	celery -A app.infrastructure.celery_app.app:celery_app worker -Q invoices -l INFO --concurrency=2

ui:
	streamlit run app/presentation/streamlit/app.py --server.port 8501 --server.headless true
