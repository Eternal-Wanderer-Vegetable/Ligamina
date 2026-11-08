# AstrBot tool-level runtime (AB-05): the AB-04 AstrBot runtime image with only the
# Core binary replaced by the current sources (AstrBot tool sessions, non-ASCII
# command tool names). The AstrBot virtual environment, Python and the hash-locked
# dependencies stay byte-identical to the AB-04 image.
ARG BASE_IMAGE=ligamina-astrbot-runtime@sha256:79903467bb35c50018deea736751db56f988d5a996246ba6fe84b34b4a28bdbc
ARG RUST_IMAGE=rust:1.97.1-slim-bookworm@sha256:39f68a3e8e3ff425f8945ffa91128e60ff930d53e17fbb5214e95824bdd46f1b

FROM ${RUST_IMAGE} AS core-build
WORKDIR /src
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY crates ./crates
COPY contracts/v0.1 ./contracts/v0.1
COPY contracts/v0.2 ./contracts/v0.2
# Report schemas compiled into the CLI; behavior Oracles (contracts/lab/automation) stay out.
COPY contracts/lab/v1 ./contracts/lab/v1
RUN cargo build --release --locked -p ligamina-cli

FROM ${BASE_IMAGE}
USER 0:0
COPY --from=core-build /src/target/release/ligamina-cli /usr/local/bin/ligamina-cli
USER 10001:10001
