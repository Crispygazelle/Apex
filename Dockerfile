FROM python:3.11-slim-bookworm

WORKDIR /out/apex

COPY pyproject.toml ./
COPY app ./app
COPY config ./config

RUN pip install --no-cache-dir . "uvicorn[standard]"

EXPOSE 8000

CMD ["python", "-m", "app.main", "--dashboard"]
