#!/usr/bin/env bash
# Follow the logs.   scripts/logs.sh            everything
#                    scripts/logs.sh backend worker   only these services
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
compose logs -f --tail 200 "$@"
