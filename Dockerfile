FROM python:3.11-slim-bookworm

WORKDIR /out/apex

COPY pyproject.toml ./
COPY app ./app
COPY config ./config

RUN pip install --no-cache-dir . "uvicorn[standard]"

EXPOSE 8000

# Dashboard stays on: config/apex.yaml already enables it. --dashboard repeats
# that so this line is obvious. Voice is also on in the YAML, and the speech
# models are not in this image, so --no-voice keeps startup from failing.
CMD ["python", "-m", "app.main", "--dashboard", "--no-voice"]
