.PHONY: build test deploy bump release logs shell help

build: ## Build and start the container
	docker compose up -d --build

test: ## Run Phase 0 + pytest (skip build)
	./ops/deploy_local.sh --skip-build

deploy: ## Full pipeline: build + Phase 0 + pytest
	./ops/deploy_local.sh

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
	@grep -E '^[a-z][a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'
