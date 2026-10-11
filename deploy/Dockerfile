FROM python:3.12-slim

WORKDIR /app

# Build the checked-out candidate, including its migration command. Copy only
# package inputs, never deployment .env files, databases, or repository metadata.
COPY pyproject.toml README.md LICENSE ./
COPY api ./api
COPY evaldiff_cli ./evaldiff_cli
RUN pip install --no-cache-dir . "psycopg[binary]>=3.1" boto3

# non-root
RUN useradd -m appuser
USER appuser

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["evaldiff-server"]
