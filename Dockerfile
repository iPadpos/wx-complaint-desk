FROM python:3.12-slim-bookworm AS wheels
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends build-essential default-libmysqlclient-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /build/requirements.txt
RUN python -m pip wheel --no-cache-dir --index-url https://mirrors.aliyun.com/pypi/simple/ \
    --wheel-dir=/wheels -r /build/requirements.txt

FROM python:3.12-slim-bookworm
RUN sed -i 's|http://deb.debian.org/debian|https://mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends libmariadb3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 complaint && useradd --uid 10001 --gid 10001 --no-create-home complaint
COPY --from=wheels /wheels /wheels
COPY requirements.txt /build/requirements.txt
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r /build/requirements.txt \
    && rm -rf /wheels /build
WORKDIR /app
COPY --chown=complaint:complaint manage.py ./
COPY --chown=complaint:complaint config ./config
COPY --chown=complaint:complaint desk ./desk
COPY --chown=complaint:complaint templates ./templates
COPY --chown=complaint:complaint static ./static
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN mkdir -p /app/staticfiles && chown complaint:complaint /app/staticfiles
USER complaint
RUN SECRET_KEY=static-build DATA_KEY=static-build python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-"]
