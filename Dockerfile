FROM python:3.12-slim

# Set WITH_BROWSER=1 to include Playwright's Chromium for the optional browser (adds ~400 MB).
ARG WITH_BROWSER=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AGENTWEBVIEW_DATA=/data \
    AGENTWEBVIEW_HOST=0.0.0.0 \
    AGENTWEBVIEW_ALLOW_REMOTE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir . \
 && if [ "$WITH_BROWSER" = "1" ]; then \
      pip install --no-cache-dir "playwright>=1.48" && playwright install --with-deps chromium; \
    fi \
 && useradd --create-home --uid 10001 agentwebview \
 && mkdir -p /data && chown agentwebview /data

# Chromium's own sandbox needs kernel features containers usually lack; the container is the boundary here.
ENV AGENTWEBVIEW_CHROMIUM_NO_SANDBOX=1
USER agentwebview
VOLUME ["/data"]
EXPOSE 8718
# Listening on 0.0.0.0 inside the container is allowed because the container's port is published on 127.0.0.1
# only (see docker-compose.yml). Do not publish it on a public interface: the dashboard has no login.
CMD ["agentwebview", "serve"]
