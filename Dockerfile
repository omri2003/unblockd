# Pinned to Debian release + Python minor (a floating tag changes under you); pin a @sha256 digest for fully reproducible builds.
FROM python:3.12-slim-bookworm
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY unblockd ./unblockd
COPY static ./static
# OpenShift pattern: runs as 1001, or any random UID it assigns (always in group 0), so group perms = owner perms.
# No /data/tickets here: TICKETS_DIR is the first board, so an empty one would become the default "Tickets" board.
RUN mkdir -p /data /projects && chown -R 1001:0 /app /data /projects && chmod -R g=u /app /data /projects
USER 1001
ENV UNBLOCKD_ROOT=/projects TICKETS_DIR=/data/tickets HOST=0.0.0.0 PORT=8787 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8787 8788
# The board's port (PORT). The MCP service shares this image and disables it in compose.yaml.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD ["python", "-c", "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ['PORT'], timeout=3)"]
# Board by default. Shared MCP server (compose runs it): python -m unblockd.mcp_server --http  (:8788/mcp)
# MCP over stdio: docker run -i --rm -v <projects folder>:/projects unblockd python -m unblockd.mcp_server
CMD ["python", "-m", "unblockd.web"]
