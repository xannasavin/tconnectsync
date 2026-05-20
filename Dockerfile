# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm AS base

# Apply latest Debian security patches on top of the base image, since the
# python:3.12-slim-bookworm tag is rebuilt less frequently than Debian
# security advisories are issued. Keeps the runtime free of fixed-upstream
# CVEs (Trivy findings) without waiting for a python base refresh.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get -y upgrade \
 && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS python-deps

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip pipenv

WORKDIR /base

COPY Pipfile Pipfile.lock setup.cfg setup.py pyproject.toml README.md ./
COPY tconnectsync/ ./tconnectsync/
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy

FROM base AS runtime

ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown
ENV TCONNECTSYNC_REVISION=$GIT_SHA \
    TCONNECTSYNC_BUILD_DATE=$BUILD_DATE

COPY --from=python-deps /base/.venv /base/.venv
ENV PATH="/base/.venv/bin:$PATH"

RUN useradd --create-home appuser
WORKDIR /home/appuser
USER appuser

COPY --chown=appuser:appuser . .

ENTRYPOINT ["python3", "-u", "main.py"]
