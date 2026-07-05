FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends apt-mirror ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY sync.sh /sync.sh
RUN chmod +x /sync.sh
CMD ["/sync.sh"]
