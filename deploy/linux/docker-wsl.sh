#!/usr/bin/env bash
set -euo pipefail

docker_command="$(command -v docker || true)"
if [[ "$docker_command" == /mnt/* ]]; then
  # WSL PATH may contain Docker Desktop's integration shim even when that
  # distribution has not enabled Docker integration. Use the Windows CLI below.
  docker_command=""
fi
if [[ -z "$docker_command" ]]; then
  docker_command="/mnt/c/Program Files/Docker/Docker/resources/bin/docker.exe"
fi
if [[ ! -x "$docker_command" ]]; then
  echo "Docker CLI is unavailable in WSL; install Docker Desktop integration or Docker Engine." >&2
  exit 2
fi
exec "$docker_command" "$@"
