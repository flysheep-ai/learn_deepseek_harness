# learn-agent-harness — developer entry points.
# Run `make help` for an overview. All chapter demos run offline (no API key).

PY ?= python3
CHAPTERS := s01_agent_loop s02_tool_use s03_tool_registry s04_permission \
	s05_session_event_log s06_turn_and_step s07_prompt_assembly s08_skill_loading \
	s09_subagent s10_context_compaction s11_task_system s12_background_jobs \
	s13_event_bus s14_plugin_system s15_capability_seams s16_agent_team \
	s17_goal_loop s18_full_harness

.PHONY: help install install-dev test lint lint-fix demo demo-all clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install runtime dependencies
	$(PY) -m pip install -r requirements.txt

install-dev: ## Install runtime + development dependencies
	$(PY) -m pip install -r requirements.txt -r requirements-dev.txt

test: ## Run the full offline test suite (unittest)
	$(PY) -m unittest discover tests

lint: ## Run static checks (ruff)
	ruff check .

lint-fix: ## Auto-fix lint issues where safe (ruff)
	ruff check . --fix

demo: ## Run one chapter offline, e.g. `make demo CH=s18_full_harness`
	$(PY) $(CH)/code.py --demo

demo-all: ## Run every chapter's offline demo
	@for d in $(CHAPTERS); do \
		echo "== $$d =="; \
		$(PY) $$d/code.py --demo >/dev/null || exit 1; \
	done
	@echo "all 18 chapters OK"

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
