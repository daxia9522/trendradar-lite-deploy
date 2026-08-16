FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOCKER_CONTAINER=true \
    STORAGE_BACKEND=local \
    TZ=Asia/Shanghai

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 1000 trendradar

COPY trendradar ./trendradar
COPY weekly_report ./weekly_report
COPY config ./config
COPY deploy/docker ./deploy/docker
COPY deploy/configure.py ./deploy/configure.py
COPY LICENSE README.md ./

RUN mkdir -p /app/output \
    && chown -R trendradar:trendradar /app

USER trendradar

HEALTHCHECK --interval=5m --timeout=30s --start-period=30s --retries=3 \
  CMD python -m trendradar --doctor >/dev/null || exit 1

ENTRYPOINT ["python", "deploy/docker/scheduler.py"]
