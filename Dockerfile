FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN addgroup --system --gid 10001 solarview \
    && adduser --system --uid 10001 --ingroup solarview --home /app solarview

COPY --chown=solarview:solarview . /app

RUN python -m pip install --upgrade pip \
    && python -m pip install . \
    && mkdir -p /app/staticfiles \
    && chown -R solarview:solarview /app

USER solarview

EXPOSE 8000

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-"]
