FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY unblockd ./unblockd
COPY static ./static
ENV TICKETS_DIR=/data/tickets HOST=0.0.0.0 PORT=8787 PYTHONUNBUFFERED=1
EXPOSE 8787
# Board by default. MCP server: docker run -i --rm -v <project>:/data unblockd python -m unblockd.mcp_server
CMD ["python", "-m", "unblockd.web"]
