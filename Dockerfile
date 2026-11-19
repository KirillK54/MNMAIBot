FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY vpi_bot ./vpi_bot
COPY migrations ./migrations
COPY alembic.ini ./
RUN pip install --no-cache-dir .
CMD ["python", "-m", "vpi_bot.main"]
