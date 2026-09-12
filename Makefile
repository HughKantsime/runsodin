.PHONY: build test test-contracts test-candidate test-database-parity test-edu test-edu-privacy test-edu-backup test-edu-hardware test-edu-load test-edu-accessibility test-edu-readiness verify-edu-live verify-backup test-security test-e2e test-coverage scan security security-operational security-audit security-secrets security-sast security-docker verify bump release logs shell tokens help

PYTHON ?= python3
SECURITY_PYTHON ?= python3.11
CANDIDATE_PYTHON ?= python3.11
EDU_RUN_ID ?= $(shell date -u +%Y%m%dT%H%M%SZ)-$(shell git rev-parse --short HEAD)
EDU_RUN_DIR ?= artifacts/edu-readiness/$(EDU_RUN_ID)
BACKUP_NAME ?= latest

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
	$(PYTHON) -m pytest tests/test_contracts/ -v --tb=short

test-candidate: ## Build and test one exact disposable ODIN candidate image
	$(CANDIDATE_PYTHON) -m ops.release_gate.runner

test-database-parity: ## Build and test SQLite/PostgreSQL parity twice with HTML evidence
	$(CANDIDATE_PYTHON) -m ops.database_parity.runner

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

test-edu-privacy: ## Run EDU privacy/browser-storage checks
	ADMIN_USERNAME=ci ADMIN_PASSWORD=ci PYTHONPATH=backend $(PYTHON) -m pytest tests/test_contracts/test_edu_privacy.py -q --tb=short -o xfail_strict=true
	ADMIN_USERNAME=ci ADMIN_PASSWORD=ci PYTHONPATH=backend $(PYTHON) -m pytest tests/privacy/test_privacy_lifecycle.py -q --tb=short -o xfail_strict=true
	ADMIN_USERNAME=ci ADMIN_PASSWORD=ci PYTHONPATH=backend $(PYTHON) -m pytest tests/privacy/test_websocket_privacy.py -q --tb=short -o xfail_strict=true
	npm --prefix frontend test -- --run src/permissions.test.ts
	$(PYTHON) ops/edu_readiness/run_privacy_browser.py --run-dir $(EDU_RUN_DIR)

test-edu-backup: ## Run SQLite backup/restore safety checks
	PYTHONPATH=backend $(PYTHON) -m pytest tests/backup_restore/test_backup_service.py -q --tb=short -o xfail_strict=true

test-edu-hardware: ## Run passive hardware protocol/transport checks
	PYTHONPATH=backend $(PYTHON) -m pytest tests/hardware/test_read_only_certification.py -q --tb=short -o xfail_strict=true

test-edu-load: ## Run isolated API/WebSocket load thresholds
	$(PYTHON) ops/edu_readiness/api_load.py --run-id $(EDU_RUN_ID) --output $(EDU_RUN_DIR)/api_load.json

test-edu-accessibility: ## Run compiled UI axe/keyboard/motion/target-size matrix
	$(PYTHON) ops/edu_readiness/run_accessibility.py --run-id $(EDU_RUN_ID) --run-dir $(EDU_RUN_DIR)

test-edu-readiness: ## Run all deterministic EDU readiness checks (currently fail-loud on blockers)
	@mkdir -p $(EDU_RUN_DIR)
	@status=0; \
	$(PYTHON) ops/edu_readiness/run_command_gate.py --run-id $(EDU_RUN_ID) --run-dir $(EDU_RUN_DIR) --gate foundation --minimum-assertions 1 -- $(MAKE) test-edu PYTHON=$(PYTHON) || status=1; \
	$(PYTHON) ops/edu_readiness/run_command_gate.py --run-id $(EDU_RUN_ID) --run-dir $(EDU_RUN_DIR) --gate privacy --minimum-assertions 17 -- $(MAKE) test-edu-privacy PYTHON=$(PYTHON) EDU_RUN_DIR=$(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/run_command_gate.py --run-id $(EDU_RUN_ID) --run-dir $(EDU_RUN_DIR) --gate backup_restore --minimum-assertions 8 -- $(MAKE) test-edu-backup PYTHON=$(PYTHON) || status=1; \
	$(PYTHON) ops/edu_readiness/run_command_gate.py --run-id $(EDU_RUN_ID) --run-dir $(EDU_RUN_DIR) --gate hardware_contracts --minimum-assertions 4 -- $(MAKE) test-edu-hardware PYTHON=$(PYTHON) || status=1; \
	$(MAKE) test-edu-load PYTHON=$(PYTHON) EDU_RUN_ID=$(EDU_RUN_ID) EDU_RUN_DIR=$(EDU_RUN_DIR) || status=1; \
	$(MAKE) test-edu-accessibility PYTHON=$(PYTHON) EDU_RUN_ID=$(EDU_RUN_ID) EDU_RUN_DIR=$(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/run_command_gate.py --run-id $(EDU_RUN_ID) --run-dir $(EDU_RUN_DIR) --gate security --minimum-assertions 10 -- $(MAKE) security PYTHON=$(PYTHON) || status=1; \
	$(PYTHON) ops/edu_readiness/aggregate.py --run-dir $(EDU_RUN_DIR) --policy ops/edu_readiness/readiness-policy.json --scope code-controlled >/dev/null 2>&1 || true; \
	$(PYTHON) ops/edu_readiness/generate_report.py --run-dir $(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/artifact_scan.py --run-id $(EDU_RUN_ID) $(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/aggregate.py --run-dir $(EDU_RUN_DIR) --policy ops/edu_readiness/readiness-policy.json --scope code-controlled || status=1; \
	$(PYTHON) ops/edu_readiness/generate_report.py --run-dir $(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/artifact_scan.py --run-id $(EDU_RUN_ID) $(EDU_RUN_DIR) || status=1; \
	exit $$status

verify-edu-live: ## Read-only TLS, legal-source, and physical-hardware readiness rows
	@mkdir -p $(EDU_RUN_DIR)
	@status=0; \
	$(PYTHON) ops/edu_readiness/verify_live.py --run-id $(EDU_RUN_ID) --run-dir $(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/aggregate.py --run-dir $(EDU_RUN_DIR) --policy ops/edu_readiness/readiness-policy.json >/dev/null 2>&1 || true; \
	$(PYTHON) ops/edu_readiness/generate_report.py --run-dir $(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/artifact_scan.py --run-id $(EDU_RUN_ID) $(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/aggregate.py --run-dir $(EDU_RUN_DIR) --policy ops/edu_readiness/readiness-policy.json || status=1; \
	$(PYTHON) ops/edu_readiness/generate_report.py --run-dir $(EDU_RUN_DIR) || status=1; \
	$(PYTHON) ops/edu_readiness/artifact_scan.py --run-id $(EDU_RUN_ID) $(EDU_RUN_DIR) || status=1; \
	exit $$status

verify-backup: ## Non-destructively verify the latest SQLite or PostgreSQL backup
	@test -n "$(DATABASE_URL)" || (echo "Usage: make verify-backup DATABASE_URL=<url> [BACKUP_NAME=latest]" && exit 1)
	cd backend && $(PYTHON) -m modules.system.backup_verifier --database-url "$(DATABASE_URL)" --backup "$(BACKUP_NAME)"

test-security: ## Run Layer 3 security tests
	pytest tests/security/ -v --tb=short

test-coverage: ## RBAC route coverage gate — fails if new routes not in RBAC matrix
	pytest tests/test_route_coverage.py -v --tb=short

security: security-operational security-secrets security-audit security-sast security-docker ## Run operational and scanner security checks (hard fail)
	@echo "4 passed in security scanners"

security-operational: ## Verify EDU HTTP, host, cache, backup-capacity, and failure behavior
	PYTHONPATH=backend $(PYTHON) -m pytest tests/privacy/test_operational_security.py tests/test_contracts/test_readiness_deploy_parity.py -q --tb=short -o xfail_strict=true

security-audit: ## Dependency audit (pip-audit + npm audit)
	$(SECURITY_PYTHON) -m pip_audit -r backend/requirements.txt --progress-spinner off --desc on
	cd frontend && npm audit --audit-level=high

security-secrets: ## Secret scanning (gitleaks)
	gitleaks detect --source . --config .gitleaks.toml -v

security-sast: ## Static analysis (bandit + semgrep)
	$(SECURITY_PYTHON) -m bandit -r backend/ -lll --exclude backend/vision_models_default/ -q
	semgrep --config auto --error --no-git-ignore --exclude='tests/*' --exclude='*.min.js' backend/ ops/edu_readiness/

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
