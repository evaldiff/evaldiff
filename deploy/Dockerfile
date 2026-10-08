FROM python:3.12-slim

WORKDIR /app

# evaldiff 0.0.12 from PyPI (API + CLI in one package)
# + drivers: psycopg (Postgres), boto3 (S3/SeaweedFS)
RUN pip install --no-cache-dir evaldiff==0.0.12 "psycopg[binary]>=3.1" boto3

# non-root
RUN useradd -m appuser
USER appuser

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["evaldiff-server"]
