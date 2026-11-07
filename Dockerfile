FROM signaldeck:dev

WORKDIR /app
COPY pyproject.toml README.md ./
COPY plugins ./plugins

# The base image already includes signaldeck-sdk and signaldeck-plugin-main.
# Install the merged Socket.IO plugin for live @haus demos in the same server.
# Demo configuration is mounted from ./config; it is not part of the wheel.
RUN pip install --no-cache-dir \
    "mcp>=1.30,<2" \
    "uvicorn>=0.35,<1" \
    "httpx>=0.27,<1" \
    "jsonschema>=4.26,<5" \
    "flask-socketio>=5.5,<6" \
    "simple-websocket>=1.1,<2" \
    && pip install --no-cache-dir \
        "git+https://github.com/signaldeck/signaldeck-plugin-sqlite.git@main" \
    && pip install --no-cache-dir --no-deps \
        "git+https://github.com/signaldeck/signaldeck-plugin-socketio.git@main" \
    && pip install --no-cache-dir --no-deps .

ENV PYTHONPATH=/plugins
