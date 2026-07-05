FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# tcpdump only present for debugging from inside the container if needed.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 tcpdump \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY probe.py ./

EXPOSE 8090
CMD ["python", "probe.py"]
