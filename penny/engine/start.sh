#!/usr/bin/env bash
# Starts the GOWA engine — the WhatsApp Web protocol client that the UI talks to.
#
# The `cd` below is load-bearing: GOWA resolves storages/ (session keys AND the
# message database) relative to the working directory, and chatstorage.db has no
# override flag. Launch the binary from anywhere else and you get a fresh, empty
# store — i.e. a silently unlinked account.
set -euo pipefail

cd "$(dirname "$0")"
set -a; source ../.env; set +a

# DEBUG=true streams whatsmeow's protocol-level logs (raw WhatsApp nodes in/out).
DEBUG="${DEBUG:-false}"

exec ./gowa rest \
  --port="${GOWA_PORT}" \
  --basic-auth="${GOWA_USER}:${GOWA_PASS}" \
  --webhook="${WEBHOOK_URL}" \
  --webhook-secret="${WEBHOOK_SECRET}" \
  --os="Penny" \
  --account-validation=false \
  --auto-mark-read=false \
  --debug="${DEBUG}"
