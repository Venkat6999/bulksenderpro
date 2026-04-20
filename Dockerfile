# Use the official Playwright image which comes pre-installed with all browser dependencies
FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy

WORKDIR /app

# Install Python dependencies first (for better caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers inside the container
# This ensures Chromium is ready to go out of the box
RUN playwright install chromium

# Copy the rest of your code
COPY . .

# Ensure the /data directory exists for persistent WhatsApp sessions
RUN mkdir -p /data && chmod 777 /data

# Set environment variables for Render
ENV PORT=3000 \
    DOCKER=true \
    RENDER=true \
    PYTHONUNBUFFERED=1

EXPOSE 3000

# Start the server using Gunicorn with gthread worker
# gthread is more stable than eventlet for Playwright (avoids event loop conflicts)
CMD ["gunicorn", "--worker-class", "gthread", "-w", "1", "--threads", "4", "--bind", "0.0.0.0:3000", "server:app"]
