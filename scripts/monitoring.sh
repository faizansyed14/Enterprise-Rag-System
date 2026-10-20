#!/usr/bin/env bash
# Optional Prometheus + Grafana (the built-in Monitor page needs neither).
#   scripts/monitoring.sh start | stop | status
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
ensure_env
case "${1:-start}" in
  start)
    compose --profile monitoring up -d prometheus grafana
    prometheus_port="$(env_get PROMETHEUS_PORT)"
    grafana_port="$(env_get GRAFANA_PORT)"
    bold "Prometheus: http://localhost:${prometheus_port:-9090}"
    bold "Grafana:    http://localhost:${grafana_port:-3001}  (user admin, password GRAFANA_ADMIN_PASSWORD from .env)"
    note "Dashboard: ALAIN > ALAIN overview. Both listen on localhost only."
    ;;
  stop)
    compose --profile monitoring stop prometheus grafana
    compose --profile monitoring rm -f prometheus grafana
    bold "Prometheus and Grafana stopped (their data is kept)."
    ;;
  status)
    compose --profile monitoring ps prometheus grafana
    ;;
  *) die "Usage: scripts/monitoring.sh start|stop|status" ;;
esac
