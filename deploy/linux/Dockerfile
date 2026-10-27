# syntax=docker/dockerfile:1.7
ARG RUST_IMAGE=rust:1.97.1-slim-bookworm@sha256:39f68a3e8e3ff425f8945ffa91128e60ff930d53e17fbb5214e95824bdd46f1b
ARG PYTHON_IMAGE=python:3.12.14-slim-bookworm@sha256:1aaa65a85fda306ffb8b910824d4e93bdce61e212c7e87168123ea3073b41a1a
ARG NODE_IMAGE=node:24.20.0-bookworm-slim@sha256:6642ef280aebc09c4541bee0b15c9f89f0f3f3c247ddee79ae1d37eddfdcbbaa

FROM ${RUST_IMAGE} AS core-build
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY crates ./crates
COPY contracts ./contracts
RUN cargo build --release --locked -p ligamina-cli

FROM ${PYTHON_IMAGE} AS python-host
WORKDIR /opt/ligamina/hosts/nonebot
ADD --checksum=sha256:68a509da24b06b4223a1c0175fb5eb5bc79342b76cbeff0cfe51ac3f5b17b6b2 https://releases.astral.sh/github/uv/releases/download/0.12.5/uv-x86_64-unknown-linux-gnu.tar.gz /tmp/uv.tar.gz
RUN tar -xzf /tmp/uv.tar.gz -C /tmp \
    && install -m 0755 /tmp/uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv \
    && install -m 0755 /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/uvx \
    && rm -rf /tmp/uv.tar.gz /tmp/uv-x86_64-unknown-linux-gnu
COPY hosts/nonebot/pyproject.toml hosts/nonebot/uv.lock ./
COPY hosts/nonebot/ligamina_nonebot ./ligamina_nonebot
RUN uv sync --locked --no-dev --python 3.12.14

FROM ${NODE_IMAGE} AS koishi-host
WORKDIR /opt/ligamina/hosts/koishi
COPY hosts/koishi/package.json hosts/koishi/package-lock.json ./
RUN npm ci
COPY hosts/koishi/tsconfig.json ./
COPY hosts/koishi/src ./src
RUN npm run build && npm prune --omit=dev

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 NODE_ENV=production
WORKDIR /opt/ligamina
COPY --from=core-build /src/target/release/ligamina-cli /usr/local/bin/ligamina-cli
COPY --from=python-host /opt/ligamina/hosts/nonebot /opt/ligamina/hosts/nonebot
COPY --from=koishi-host /usr/local/bin/node /usr/local/bin/node
COPY --from=koishi-host /opt/ligamina/hosts/koishi/dist /opt/ligamina/hosts/koishi/dist
COPY --from=koishi-host /opt/ligamina/hosts/koishi/node_modules /opt/ligamina/hosts/koishi/node_modules
COPY --from=koishi-host /opt/ligamina/hosts/koishi/package.json /opt/ligamina/hosts/koishi/package.json
COPY --from=koishi-host /opt/ligamina/hosts/koishi/package-lock.json /opt/ligamina/hosts/koishi/package-lock.json
COPY contracts ./contracts
COPY deploy/linux/entrypoint.py deploy/linux/healthcheck.py /opt/ligamina/deploy/linux/
COPY deploy/linux/daemon.example.json /etc/ligamina/daemon.example.json
RUN groupadd --gid 10001 ligamina \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin ligamina \
    && mkdir -p /var/lib/ligamina /run/ligamina \
    && chown -R 10001:10001 /var/lib/ligamina /run/ligamina /opt/ligamina
USER 10001:10001
EXPOSE 26701
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 CMD ["python", "/opt/ligamina/deploy/linux/healthcheck.py"]
ENTRYPOINT ["python", "/opt/ligamina/deploy/linux/entrypoint.py"]
