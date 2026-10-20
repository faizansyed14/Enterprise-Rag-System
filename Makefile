# Shortcuts for the scripts in scripts/ (run `make help`). Every target is a thin wrapper, so the scripts work
# without make too.
.DEFAULT_GOAL := help
.PHONY: help start stop restart status logs demo eval models monitoring-start monitoring-stop backup dev test \
	test-backend test-frontend admin

help: ## Show the targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-18s\033[0m %s\n", $$1, $$2}'

start: ## Build and start the stack (first run creates .env and downloads the local models)
	scripts/start.sh
stop: ## Stop the stack (data is kept)
	scripts/stop.sh
restart: ## Recreate the application containers after changing .env
	scripts/restart.sh
status: ## Containers, configuration check and queue
	scripts/status.sh
logs: ## Follow all logs (make logs s="backend worker" for some)
	scripts/logs.sh $(s)
demo: ## Load the sample library and demo users
	scripts/demo.sh
eval: ## Score the demo golden set
	scripts/eval.sh
models: ## Download the models in LOCAL_MODELS
	scripts/models.sh pull
monitoring-start: ## Start Prometheus and Grafana
	scripts/monitoring.sh start
monitoring-stop: ## Stop Prometheus and Grafana
	scripts/monitoring.sh stop
backup: ## Back up the database and originals to backups/
	scripts/backup.sh
admin: ## Create an administrator: make admin email=you@company.com
	scripts/admin.sh $(email)
dev: ## Run from source with hot reload
	scripts/dev.sh
test: ## Lint, tests and frontend build
	scripts/test.sh
test-backend: ## Backend lint and tests only
	scripts/test.sh backend
test-frontend: ## Frontend typecheck, lint and build only
	scripts/test.sh frontend
