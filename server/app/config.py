import os


class Settings:
    database_url = os.environ.get("DATABASE_URL", "sqlite:///./landscape.db")
    session_secret = os.environ.get("SESSION_SECRET", "dev-secret")
    admin_username = os.environ.get("ADMIN_USERNAME", "admin")
    admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
    server_ip = os.environ.get("SERVER_IP", "192.168.1.125")
    subnet = os.environ.get("SUBNET", "192.168.1.0")
    http_port = int(os.environ.get("HTTP_PORT", "8081"))
    web_port = int(os.environ.get("WEB_PORT", "8443"))
    probe_url = os.environ.get("PROBE_URL", "http://host.docker.internal:8090")
    dnsmasq_container = os.environ.get("DNSMASQ_CONTAINER", "landscape-dnsmasq")

    http_dir = os.environ.get("RENDER_HTTP_DIR", "/srv/http")
    smb_dir = os.environ.get("RENDER_SMB_DIR", "/srv/smb")
    repo_dir = os.environ.get("REPO_DIR", "/srv/repo")
    agent_dir = os.environ.get("AGENT_DIR", "/srv/agent")
    dnsmasq_conf = os.environ.get(
        "RENDER_DNSMASQ_CONF", "/srv/config/dnsmasq/dnsmasq.conf"
    )
    docker_sock = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")


settings = Settings()
