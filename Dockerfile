FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# Docker Official Image multi-platform index (linux/amd64 + linux/arm64).
# Before updating: re-verify the index digest and both platform manifests at the
# registry; a version tag alone remains mutable and security fixes do not flow in.

LABEL org.trendradar.runtime-config="1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=true \
    STORAGE_BACKEND=local \
    TZ=Asia/Shanghai \
    LITELLM_LOCAL_MODEL_COST_MAP=True

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock \
    && useradd --create-home --uid 1000 trendradar

COPY trendradar ./trendradar
COPY weekly_report ./weekly_report
COPY config ./config
COPY deploy/docker ./deploy/docker
COPY deploy/*.py ./deploy/
COPY .env.example ./
COPY LICENSE README.md ./

RUN mkdir -p /app/output \
    && chown -R trendradar:trendradar /app

USER trendradar

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
  CMD python deploy/docker/entrypoint.py doctor >/dev/null || exit 1

ENTRYPOINT ["python", "deploy/docker/entrypoint.py"]
CMD ["schedule"]
