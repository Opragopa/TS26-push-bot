FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV TS26_APP_VERSION=container

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Dependencies are already installed above, so the runtime bootstrap has nothing
# to do here; disabling it keeps the container from attempting pip at startup.
ENV TS26_SKIP_DEPENDENCY_BOOTSTRAP=1

# Run as an unprivileged user. The bot needs to write only its state directory.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 CMD ["python", "healthcheck.py"]

CMD ["python", "main.py"]
