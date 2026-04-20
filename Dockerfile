FROM mcr.microsoft.com/playwright/python:v1.51.0-jammy
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium
COPY . .
RUN mkdir -p /data && chmod 777 /data
ENV PORT=3000 \
    DOCKER=true \
    RENDER=true \
    PYTHONUNBUFFERED=1
EXPOSE 3000
CMD ["gunicorn", "--worker-class", "gthread", "-w", "1", "--threads", "4", "--bind", "0.0.0.0:3000", "server:app"]
