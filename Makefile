# Small, honest entry points. `make score` exists because OpenSSF Scorecard
# grades the working tree: running Python leaves __pycache__ behind, and any
# .pyc makes Binary-Artifacts drop to 0 even though it is git-ignored.

PYTHON ?= python3
SCORECARD ?= scorecard

.DEFAULT_GOAL := help

help: ## Show this help
	@grep -hE '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

test: ## Run the unit tests (no bytecode left behind)
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) -m unittest discover -s tests -q

fuzz: ## Fuzz the date parser for 60s (needs: pip install -r fuzz/requirements.txt)
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) fuzz/fuzz_dates.py -max_total_time=60 -print_final_stats=1

clean: ## Remove bytecode and fuzzing artifacts
	find . -path ./.git -prune -o -name "__pycache__" -type d -exec rm -rf {} +
	rm -rf fuzz/corpus fuzz/crash-* crash-* .coverage

score: clean ## OpenSSF Scorecard from a clean tree
	$(SCORECARD) --local ./

docker-worker: ## Build the isolated worker image
	docker compose build eventure

docker-collector: ## Build the Instagram collector image
	docker compose --profile instagram build instagram

.PHONY: help test fuzz clean score docker-worker docker-collector
