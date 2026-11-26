# syntax=docker/dockerfile:1
#
# Two targets, one file:
#   base  the worker: stdlib only, no dependencies, small on an SD card
#   ig    the same image plus instagrapi, for the disposable social collector
#
# Build the worker:  docker build --target base -t eventure:latest .
# Build the collector: docker build --target ig -t eventure:ig .

FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# Debian slim does not always carry a CA bundle; without it every HTTPS call
# fails with CERTIFICATE_VERIFY_FAILED. Cheap insurance, ~200 KB.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY audit.py ./
COPY eventure ./eventure
COPY ig_collect.py ./
COPY config.example.toml ./

# UID 1000 matches the default first user on Debian/Raspberry Pi OS, so the
# bind-mounted ./data is writable without chown gymnastics.
RUN useradd --uid 1000 --user-group --create-home --shell /usr/sbin/nologin eventure \
    && mkdir -p /app/data \
    && chown -R eventure:eventure /app

USER eventure

# Scheduled digests + Telegram button handling, in one process.
CMD ["python", "-m", "eventure", "serve"]


FROM base AS ig

USER root
# instagrapi talks to the private mobile API. It is a third-party, unofficial
# client: dependencies come from a lockfile with hashes, so a swapped wheel
# fails the build instead of running. Dependabot refreshes the pin.
COPY requirements-ig.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements-ig.txt
USER eventure

CMD ["python", "ig_collect.py", "--engine", "instagrapi", "--days", "14", "--limit", "12"]
