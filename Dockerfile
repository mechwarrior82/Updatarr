FROM python:3.12-slim

WORKDIR /app

# Install Docker CLI + Compose plugin from official Docker repo
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
       > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends \
       docker-ce-cli \
       docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

# Symlink so both `docker compose` and `docker-compose` work
RUN ln -s /usr/libexec/docker/cli-plugins/docker-compose /usr/local/bin/docker-compose

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ .

EXPOSE 3001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "3001", "--workers", "1"]
