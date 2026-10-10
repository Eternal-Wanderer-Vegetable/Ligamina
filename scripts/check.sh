#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
cd "$repo_root"

# WSL images and self-hosted runners may keep the locked local toolchains outside PATH.
if [[ -x "${HOME:-}/.cargo/bin/cargo" ]]; then
  export PATH="${HOME}/.cargo/bin:$PATH"
fi
if [[ -x /opt/node-v24.20.0/bin/node ]]; then
  export PATH="/opt/node-v24.20.0/bin:$PATH"
fi

platform="linux"
rust_version="$(rustc --version)"
uv_version="$(uv --version)"
node_version="$(node --version)"
[[ "$rust_version" == "rustc 1.97.1 "* ]] || { echo "expected rustc 1.97.1; found: $rust_version" >&2; exit 2; }
[[ "$uv_version" == "uv 0.12.5"* ]] || { echo "expected uv 0.12.5; found: $uv_version" >&2; exit 2; }
[[ "$node_version" == "v24.20.0" ]] || { echo "expected Node v24.20.0; found: $node_version" >&2; exit 2; }

uv python install 3.12.14

uv run --locked --project hosts/nonebot --python 3.12.14 pytest -q hosts/nonebot/tests
uv run --locked --project hosts/nonebot --python 3.12.14 python -m unittest discover -s lab/tests -v
uv run --locked --project hosts/nonebot --python 3.12.14 python hosts/nonebot/tests/native_smoke.py
uv run --locked --project hosts/nonebot --python 3.12.14 python hosts/nonebot/tests/native_bridge.py
uv run --locked --project hosts/nonebot --python 3.12.14 python hosts/nonebot/tests/worker_smoke.py

(
  cd hosts/koishi
  npm ci
  npm test
  npm run test:mcp
)
(
  cd clients/reference
  npm ci
  npm test
)

cargo fmt --all -- --check
cargo test --workspace --locked
cargo build --release --locked

mkdir -p outputs/acceptance
uv run --locked --project lab --python 3.12.14 python lab/run.py \
  --suite smoke --platform "$platform" \
  --report "outputs/acceptance/ci-smoke-${platform}.json"
