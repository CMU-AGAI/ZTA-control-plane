FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Copy and install requirements
COPY airline-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared A2A library
COPY __init__.py /app/agents/__init__.py
COPY a2a/ /app/agents/a2a/

# Copy agent code
COPY airline-agent/agent.py .

EXPOSE 8091
CMD ["python", "agent.py"]