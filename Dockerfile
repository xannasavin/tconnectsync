# syntax=docker/dockerfile:1.7

FROM python:3.13-slim-bookworm AS base

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

RUN pip install pipenv

WORKDIR /base

COPY Pipfile Pipfile.lock setup.cfg setup.py pyproject.toml README.md ./
COPY tconnectsync/ ./tconnectsync/
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy

FROM base AS runtime

COPY --from=python-deps /base/.venv /base/.venv
ENV PATH="/base/.venv/bin:$PATH"

RUN useradd --create-home appuser
WORKDIR /home/appuser
USER appuser

COPY --chown=appuser:appuser . .

ENTRYPOINT ["python3", "-u", "main.py"]
