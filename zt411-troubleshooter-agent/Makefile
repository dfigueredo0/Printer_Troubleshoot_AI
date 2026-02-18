setup:
	poetry install
	pre-commit install

lint:
	ruff check .
	black --check .
	isort --check-only .

test:
	pytest --cov=src --cov-report=term-missing

train:
	python -m zt411_agent.train

eval:
	python -m zt411_agent.eval

serve:
	uvicorn service.app:app --reload

docker-build:
	docker build -t zt411-agent .

docker-run:
	docker run -p 8000:8000 zt411-agent
