FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --shell /bin/bash --uid 1000 app

WORKDIR /app

COPY --chown=app:app requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .

# WORKDIR /app создаётся как root:root; run.py делает Path("logs").mkdir() в cwd,
# поэтому отдаём /app и подпапки пользователю app.
RUN mkdir -p /app/logs /app/data && chown -R app:app /app

USER app

CMD ["python", "run.py"]
