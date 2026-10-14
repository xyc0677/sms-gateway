FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py index.html manifest.webmanifest icon-180.png icon-512.png ./
COPY vendor ./vendor
COPY numinfo.py ./
COPY numdb ./numdb
# Host networking. Port 18300 must stay LAN-only (firewall); the internet
# reaches the app only through Caddy, which marks those requests as public.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "18300", "--no-proxy-headers", "--no-server-header"]
