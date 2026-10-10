# Linux single-container deployment

This Compose setup runs one Ligamina Core container. The Core starts its selected NoneBot and Koishi Workers as stdio child processes. The config and database are separate: `daemon.json` is read-only and `ligamina-data` is a named persistent volume. The port is published only on host loopback. The container has a read-only root filesystem, no added capabilities, and no Docker socket mount.

The image is pinned to exact linux/amd64 Rust, CPython, and Node image digests. uv 0.12.5 is downloaded with BuildKit checksum verification; Python and Node dependencies use their committed lock files.

## Run from WSL Ubuntu 24.04

```bash
cd deploy/linux
cp .env.example .env
cp daemon.example.json daemon.json
```

Set `LIGAMINA_ADMIN_TOKEN` to the same random value in `.env` and `daemon.json`, and replace the client token in `daemon.json`. Then choose the Host set in `.env`:

```dotenv
LIGAMINA_PROFILES=nonebot
```

Start the selected profile. The service is declared in both Compose profiles; `LIGAMINA_PROFILES` controls which child Host processes the entrypoint enables. `docker-wsl.sh` uses the Linux `docker` CLI when installed, or this machine's Docker Desktop CLI through WSL interop.

```bash
bash docker-wsl.sh compose --profile nonebot up --build -d
bash docker-wsl.sh compose ps
bash docker-wsl.sh compose logs --tail=100 ligamina
```

For Koishi alone, set `LIGAMINA_PROFILES=koishi` and use `--profile koishi`. To start both, set `LIGAMINA_PROFILES=nonebot,koishi` and enable both profiles. The example configuration loads the repository's self-contained smoke plugins; configure approved plugin modules and their required grants before exposing the Core to a client.

The client endpoint is `http://127.0.0.1:26701`. The health check uses the configured admin token. Stop with the same Compose profile used to start the service: for example `bash docker-wsl.sh compose --profile nonebot down`, or enable both profiles when stopping a combined run. The named database volume remains; add `-v` only when deleting it.

## Acceptance boundary

The default example uses the repository's self-contained smoke plugins and an unconnected Gateway registration with no operation grants; it does not connect to a real chat platform. Replace all `CHANGE_ME` client and gateway tokens before connecting a Gateway. T16's WSL Linux-container build, both Host profiles, loopback bind, health check, status/doctor, capability and event paths, volume persistence, and Compose shutdown were exercised; evidence is in `outputs/acceptance/t16-linux-wsl-container-20261010.json`. The Docker Desktop `desktop-linux` engine ran the container because Docker integration is disabled inside this WSL distribution. Third-party plugin runs must use separate restricted Workers, not this trusted-user container profile.
