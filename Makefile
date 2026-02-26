.PHONY: build test test-contracts test-security test-e2e test-coverage scan verify bump release logs shell help

build: ## Build and start the container
	docker compose up -d --build

test: ## Run main + RBAC pytest suites (RBAC runs separately)
	docker exec odin python3 -c "import sqlite3; c=sqlite3.connect('/data/odin.db'); c.execute('DELETE FROM login_attempts'); c.commit(); c.close()" 2>/dev/null || true
	pytest tests/test_features.py tests/test_license.py tests/test_mqtt_linking.py tests/test_order_math.py tests/test_security.py -v --tb=short
	docker exec odin python3 -c "import sqlite3; c=sqlite3.connect('/data/odin.db'); c.execute('DELETE FROM login_attempts'); c.commit(); c.close()" 2>/dev/null || true
	pytest tests/test_rbac.py -v --tb=short
	@echo "Updating TEST_COUNT..."
	@pytest tests/test_features.py tests/test_license.py tests/test_mqtt_linking.py tests/test_order_math.py tests/test_security.py tests/test_rbac.py tests/test_printer_models.py --co -q 2>/dev/null | tail -1 | grep -oE '[0-9]+' | head -1 > TEST_COUNT

test-contracts: ## Run contract tests (module boundaries, no container required)
	pytest tests/test_contracts/ -v --tb=short

test-security: ## Run Layer 3 security tests
	pytest tests/security/ -v --tb=short

test-coverage: ## RBAC route coverage gate — fails if new routes not in RBAC matrix
	pytest tests/test_route_coverage.py -v --tb=short

scan: ## Static security scan (bandit + pip-audit + npm audit) — mirrors CI
	@echo "=== Bandit (Python static analysis) ==="
	bandit -r backend/ -ll --exclude backend/vision_models_default/ -f txt || true
	@echo ""
	@echo "=== pip-audit (Python CVEs) ==="
	pip-audit -r backend/requirements.txt --progress-spinner off --desc on || true
	@echo ""
	@echo "=== npm audit (frontend CVEs) ==="
	cd frontend && npm audit || true

test-e2e: ## Run E2E Playwright tests
	pytest tests/test_e2e/ -v --tb=short

verify: ## Run Phase 0 health checks
	./ops/phase0_verify.sh local

bump: ## Bump version (requires VERSION=X.Y.Z)
	@test -n "$(VERSION)" || (echo "Usage: make bump VERSION=X.Y.Z" && exit 1)
	./ops/bump-version.sh $(VERSION)

release: ## Bump + push (requires VERSION=X.Y.Z)
	@test -n "$(VERSION)" || (echo "Usage: make release VERSION=X.Y.Z" && exit 1)
	./ops/bump-version.sh $(VERSION) --push

logs: ## Tail container logs
	docker compose logs -f --tail=100

shell: ## Open a shell in the container
	docker exec -it odin bash

help: ## Show this help
	@grep -E '^[a-z][a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
