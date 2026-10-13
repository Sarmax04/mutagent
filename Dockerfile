# Hosted demo image (untested draft). Build: docker build -t mutagent .
# Run:   docker run -p 8000:8000 --env-file .env mutagent
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    MUTAGENT_LIMITS=1

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY examples ./examples
COPY replays ./replays
RUN pip install --no-cache-dir -e ".[web]" pytest

# run as an unprivileged user; the app never needs to write outside temp folders
RUN useradd --create-home runner
USER runner

EXPOSE 8000
# Hosts such as Render inject $PORT; fall back to 8000.
CMD ["sh", "-c", "exec mutagent serve --host 0.0.0.0 --port ${PORT:-8000}"]
