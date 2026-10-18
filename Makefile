.PHONY: help init-db seed seed-scale generate-readme test clean

PYTHON = python

help:
	@echo "Nagarkot Access Control Service"
	@echo "Commands:"
	@echo "  make seed              - Seed small deliberate dataset (~300 jobs, 20 users)"
	@echo "  make seed-scale        - Seed scale benchmark (500 users, 200,000 jobs)"
	@echo "  make generate-readme   - Refresh plain-English access lines in README"
	@echo "  make test              - Run test suite"
	@echo "  make init-db           - Create database tables"

init-db:
	$(PYTHON) run.py init-db

seed:
	$(PYTHON) run.py seed

seed-scale:
	$(PYTHON) run.py seed-scale

generate-readme:
	$(PYTHON) run.py generate-readme-access

test:
	$(PYTHON) -m pytest -v

test-cov:
	$(PYTHON) -m pytest --cov=src --cov-report=term-missing -v

serve:
	$(PYTHON) run.py serve

clean:
	rm -rf data/*.db __pycache__ tests/__pycache__ src/__pycache__ src/**/__pycache__
