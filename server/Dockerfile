FROM python:3.12-slim

# p7zip: extract ISO contents without loop mounts (image builds in-UI).
# wimtools: patch WinPE boot.wim for unattended network installs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    p7zip-full wimtools \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
