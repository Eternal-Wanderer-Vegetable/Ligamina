#!/usr/bin/env bash
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$repo_root"
if [[ -x /opt/node-v24.20.0/bin/node ]]; then
  export PATH="/opt/node-v24.20.0/bin:$PATH"
fi
case "${1:-docs}" in
  docs) node scripts/check-docs.mjs ;;
  full)
    node scripts/check-docs.mjs
    bash scripts/check.sh
    ;;
  *) echo "Usage: bash init.sh [docs|full]" >&2; exit 2 ;;
esac
