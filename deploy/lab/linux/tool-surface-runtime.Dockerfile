# syntax=docker/dockerfile:1.7
# TOOL-04 runtime overlay: the admitted P4-01 Linux runtime image with only the Core
# binary and the Koishi Host build replaced by the current sources (command tools).
# Python, Node.js, npm and the Koishi Host dependency lock stay byte-identical to the
# admitted base, so the existing runtime probe still applies.
ARG BASE_IMAGE=ligamina-p401-runtime-offline-profile@sha256:2f2aaf3d31b522f31b1dbbc5c5e0109949a86d195e5aa491b2989b7ba9de9c5c
ARG RUST_IMAGE=rust:1.97.1-slim-bookworm@sha256:39f68a3e8e3ff425f8945ffa91128e60ff930d53e17fbb5214e95824bdd46f1b
ARG NODE_IMAGE=node:24.20.0-bookworm-slim@sha256:6642ef280aebc09c4541bee0b15c9f89f0f3f3c247ddee79ae1d37eddfdcbbaa

FROM ${RUST_IMAGE} AS core-build
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY crates ./crates
COPY contracts ./contracts
RUN cargo build --release --locked -p ligamina-cli

FROM ${NODE_IMAGE} AS koishi-host
WORKDIR /opt/ligamina/hosts/koishi
COPY hosts/koishi/package.json hosts/koishi/package-lock.json ./
RUN npm ci
COPY hosts/koishi/tsconfig.json ./
COPY hosts/koishi/src ./src
COPY contracts/v0.1 /opt/ligamina/contracts/v0.1
COPY contracts/v0.2 /opt/ligamina/contracts/v0.2
RUN npm run build

FROM ${BASE_IMAGE}
USER 0:0
COPY --from=core-build /src/target/release/ligamina-cli /usr/local/bin/ligamina-cli
RUN rm -rf /opt/ligamina/hosts/koishi/dist
COPY --from=koishi-host --chown=10001:10001 /opt/ligamina/hosts/koishi/dist /opt/ligamina/hosts/koishi/dist
# Wire contracts only: behavior Oracles (contracts/lab) must never enter the Worker image.
COPY --chown=10001:10001 contracts/v0.1 /opt/ligamina/contracts/v0.1
COPY --chown=10001:10001 contracts/v0.2 /opt/ligamina/contracts/v0.2
USER 10001:10001
