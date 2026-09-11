.PHONY: build test test-contracts test-edu test-security test-e2e test-coverage scan security security-audit security-secrets security-sast security-docker verify bump release logs shell tokens help

PYTHON ?= python3

tokens: ## Regenerate design tokens (CSS + Swift) from design/tokens.json
	node design/generate.mjs

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

test-edu: ## Run deterministic EDU backend, frontend, and Chromium release gate
	ADMIN_USERNAME=ci ADMIN_PASSWORD=ci $(PYTHON) ops/demo/run_junit_gate.py -- $(PYTHON) -m pytest \
		tests/test_license.py \
		tests/test_contracts/test_spa_auth_boundary.py \
		tests/test_contracts/test_edu_quota_gating.py \
		tests/test_contracts/test_demo_seed_edu.py \
		tests/test_contracts/test_demo_publisher_health.py \
		tests/test_contracts/test_managed_license.py \
		tests/test_contracts/test_license_preflight.py \
		tests/test_contracts/test_junit_gate.py \
		tests/test_contracts/test_demo_edu_manifests.py \
		-v --tb=short -o xfail_strict=true --junitxml={junit}
	$(PYTHON) ops/demo/run_junit_gate.py -- npm --prefix frontend test -- --run \
		src/LicenseContext.test.jsx src/components/admin/LicenseTab.test.jsx \
		--reporter=default --reporter=junit --outputFile.junit={junit}
	cd frontend && npm run build
	./ops/demo/run_edu_browser_gate.sh

test-security: ## Run Layer 3 security tests
	pytest tests/security/ -v --tb=short

test-coverage: ## RBAC route coverage gate — fails if new routes not in RBAC matrix
	pytest tests/test_route_coverage.py -v --tb=short

security: security-secrets security-audit security-sast security-docker ## Run all security checks (hard fail)

security-audit: ## Dependency audit (pip-audit + npm audit)
	pip-audit -r backend/requirements.txt --progress-spinner off --desc on
	cd frontend && npm audit --audit-level=high

security-secrets: ## Secret scanning (gitleaks)
	gitleaks detect --source . --config .gitleaks.toml -v

security-sast: ## Static analysis (bandit + semgrep)
	bandit -r backend/ -lll --exclude backend/vision_models_default/ -q
	semgrep --config auto --error --exclude='tests/*' --exclude='*.min.js' backend/

security-docker: ## Dockerfile lint (hadolint)
	hadolint Dockerfile

scan: security ## Alias for backward compatibility

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
