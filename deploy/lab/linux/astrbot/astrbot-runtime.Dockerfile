# syntax=docker/dockerfile:1.7
# AstrBot Worker runtime (AB-04/AB-05): the admitted TOOL-04 runtime image plus a
# hash-locked virtual environment containing the unmodified AstrBot 4.29.1 and its
# 155 locked dependencies. Plugin requirements are never installed at run time:
# Workers run with --network none, so AstrBot's lazy pip recovery cannot fetch anything.
# Every package must match the lock's hashes; a few locked packages (for example
# aiocqhttp 1.4.4) ship only as source distributions and are built here at image build time.
ARG BASE_IMAGE=ligamina-tool04-runtime@sha256:9c1280e40a0bfa7fb2a7135a58316624a9999ad5333ad66938eb363f7213b6b5
FROM ${BASE_IMAGE}
USER 0:0
COPY deploy/lab/linux/astrbot/requirements.lock.txt /opt/astrbot/requirements.lock.txt
RUN python -m venv /opt/astrbot/venv \
    && /opt/astrbot/venv/bin/pip install --no-cache-dir --require-hashes --no-deps \
         -r /opt/astrbot/requirements.lock.txt \
    && /opt/astrbot/venv/bin/python -c "import importlib.metadata as m; assert m.version('astrbot') == '4.29.1'" \
    && chown -R 10001:10001 /opt/astrbot
USER 10001:10001
