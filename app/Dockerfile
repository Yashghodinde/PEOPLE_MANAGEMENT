# syntax=docker/dockerfile:1
FROM node:20-alpine AS assets
WORKDIR /src
COPY app/static_src/package.json ./
RUN npm install --no-audit --no-fund
COPY app/static_src/ ./
COPY app/templates/ ../templates/
RUN mkdir -p ../static/css && npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DJANGO_SETTINGS_MODULE=config.settings.prod
RUN useradd --uid 1000 --create-home ots
WORKDIR /srv/app
COPY app/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./
COPY --from=assets /static/css/app.css ./static/css/app.css
COPY deploy/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh && python manage.py collectstatic --noinput && chown -R ots:ots /srv/app
USER ots
EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["app"]
