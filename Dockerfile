FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl nodejs npm git \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally (for OAuth session)
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY claudeclaw.py .

RUN mkdir -p /workspace/uploads
ENV CLAUDECLAW_WORKING_DIR=/workspace

CMD ["python", "claudeclaw.py"]
