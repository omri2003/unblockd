FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY unblockd ./unblockd
COPY static ./static
ENV UNBLOCKD_ROOT=/projects TICKETS_DIR=/data/tickets HOST=0.0.0.0 PORT=8787 PYTHONUNBUFFERED=1
EXPOSE 8787 8788
# Board by default. Shared MCP server (compose runs it): python -m unblockd.mcp_server --http  (:8788/mcp)
# MCP over stdio: docker run -i --rm -v <projects folder>:/projects unblockd python -m unblockd.mcp_server
CMD ["python", "-m", "unblockd.web"]
