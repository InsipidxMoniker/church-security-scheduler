FROM python:3.13-slim-bookworm@sha256:e32be0e55814a08977501ca863271d9bd02443710db8f2afaf8372e2c4508530
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock && useradd --uid 10001 --create-home scheduler && mkdir /data /keys && chown scheduler:scheduler /data /keys
COPY scheduler ./scheduler
COPY migrations ./migrations
COPY alembic.ini pytest.ini ./
COPY tests ./tests
USER 10001:10001
CMD ["uvicorn", "scheduler.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers"]
