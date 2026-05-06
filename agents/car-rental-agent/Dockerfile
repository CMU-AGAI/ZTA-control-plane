FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

COPY car-rental-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY __init__.py /app/agents/__init__.py
COPY a2a/ /app/agents/a2a/

COPY car-rental-agent/agent.py .

EXPOSE 8093
CMD ["python", "agent.py"]