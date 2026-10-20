#!/usr/bin/env bash
# Write the sample construction library (specs, drawings, RFIs, minutes in English and Arabic, a BOQ, e-mail, a
# restricted change order) and the demo users, and ingest it. Safe to run again.
#   Users: admin@demo.local, engineer@demo.local, commercial@demo.local    Password: ChangeMe-Demo-2025
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need_docker
wait_for_api 60 || die "Start the stack first: scripts/start.sh"
compose exec -T backend rag demo seed --path /data/demo "$@"
bold "Demo library queued; the workers ingest it in about a minute (Monitor page > Queues)."
note "Then sign in as engineer@demo.local and ask: What concrete grade is specified for the columns?"
note "Score it: scripts/eval.sh"
