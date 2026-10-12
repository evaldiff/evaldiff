FROM python:3.12-slim

ARG EVALDIFF_BUILD_REVISION=unknown
ARG EVALDIFF_BUILD_VERSION=unknown
LABEL org.opencontainers.image.revision=$EVALDIFF_BUILD_REVISION
LABEL org.opencontainers.image.version=$EVALDIFF_BUILD_VERSION

WORKDIR /app

# Build the checked-out candidate, including its migration command. Copy only
# package inputs, never deployment .env files, databases, or repository metadata.
COPY pyproject.toml README.md LICENSE ./
COPY api ./api
COPY evaldiff_cli ./evaldiff_cli
# Bake identity before packaging so console scripts and source imports agree.
RUN python -m api.build_info "$EVALDIFF_BUILD_REVISION" "$EVALDIFF_BUILD_VERSION" \
    && pip install --no-cache-dir . "psycopg[binary]>=3.1" boto3

# non-root
RUN useradd -m appuser
USER appuser

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["evaldiff-server"]
