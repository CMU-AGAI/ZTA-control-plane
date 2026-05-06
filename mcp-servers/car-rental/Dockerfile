FROM python:3.13-slim

WORKDIR /app

# Copy requirements first for caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose port
EXPOSE 8012

# Run in HTTP mode (no "stdio" argument)
CMD ["python", "server.py"]
