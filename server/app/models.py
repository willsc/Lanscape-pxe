import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def gen_token() -> str:
    return secrets.token_urlsafe(24)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(190), unique=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pw_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # admin/operator/viewer
    source: Mapped[str] = mapped_column(String(10), default="local")  # local/oidc
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Setting(Base):
    """Free-form key/value store: oidc config, dhcp config, pxe options."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class EnrollToken(Base):
    __tablename__ = "enroll_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, default=gen_token)
    note: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Host(Base):
    __tablename__ = "hosts"
    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[str] = mapped_column(String(128), unique=True)
    agent_key: Mapped[str] = mapped_column(String(64), default=gen_token)
    hostname: Mapped[str] = mapped_column(String(190))
    os_family: Mapped[str] = mapped_column(String(20), default="ubuntu")  # ubuntu/windows
    os_version: Mapped[str] = mapped_column(String(64), default="")
    kernel: Mapped[str] = mapped_column(String(128), default="")
    arch: Mapped[str] = mapped_column(String(32), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    mac: Mapped[str] = mapped_column(String(32), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    tags: Mapped[str] = mapped_column(String(255), default="")
    hardware: Mapped[dict] = mapped_column(JSON, default=dict)
    packages: Mapped[list] = mapped_column(JSON, default=list)
    updates: Mapped[list] = mapped_column(JSON, default=list)  # pending updates
    reboot_required: Mapped[bool] = mapped_column(Boolean, default=False)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    tasks: Mapped[list["Task"]] = relationship(back_populates="host", cascade="all, delete-orphan")


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(32))
    # types: pkg_install, pkg_remove, upgrade, script, harden,
    #        compliance_scan, repo_config, reboot
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending -> sent -> done | failed
    output: Mapped[str] = mapped_column(Text, default="")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(190), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    host: Mapped[Host] = relationship(back_populates="tasks")


class ComplianceProfile(Base):
    __tablename__ = "compliance_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(190), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    os_family: Mapped[str] = mapped_column(String(20), default="ubuntu")
    rules: Mapped[list] = mapped_column(JSON, default=list)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)


class ComplianceResult(Base):
    __tablename__ = "compliance_results"
    id: Mapped[int] = mapped_column(primary_key=True)
    host_id: Mapped[int] = mapped_column(ForeignKey("hosts.id", ondelete="CASCADE"))
    profile_id: Mapped[int] = mapped_column(ForeignKey("compliance_profiles.id", ondelete="CASCADE"))
    ran_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    results: Mapped[list] = mapped_column(JSON, default=list)

    host: Mapped[Host] = relationship()
    profile: Mapped[ComplianceProfile] = relationship()


class RepoMirror(Base):
    __tablename__ = "repo_mirrors"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(190), unique=True)
    upstream: Mapped[str] = mapped_column(String(255))  # e.g. http://archive.ubuntu.com/ubuntu
    suites: Mapped[list] = mapped_column(JSON, default=list)  # ["noble", "noble-updates", ...]
    components: Mapped[list] = mapped_column(JSON, default=list)  # ["main", "universe"]
    arches: Mapped[list] = mapped_column(JSON, default=lambda: ["amd64"])
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Image(Base):
    """An installable OS release registered with the provisioning side."""

    __tablename__ = "images"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(190), unique=True)
    os_family: Mapped[str] = mapped_column(String(20))  # ubuntu/windows
    version: Mapped[str] = mapped_column(String(32), default="")  # 22.04 / 24.04 / 26.04 / 11
    # subdirectory under http/ (ubuntu) or smb/ (windows) holding this
    # image's extracted boot assets; populated by a build job or prepare.sh.
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    default_packages: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")
    # Source ISO this image was built from (path inside the ISO library).
    iso_path: Mapped[str] = mapped_column(String(255), default="")
    # Install-time customisation; per-machine overrides take precedence.
    # ubuntu: locale keyboard timezone username password_hash storage
    #         kernel_args post_script use_local_mirror install_agent
    # windows: locale timezone_windows username password product_key
    #          image_name post_script install_agent
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class Job(Base):
    """Background work (image builds) with a live log for the UI."""

    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # image_build
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # image id
    title: Mapped[str] = mapped_column(String(190), default="")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed
    log: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Machine(Base):
    """A bare-metal target keyed by MAC for PXE provisioning."""

    __tablename__ = "machines"
    id: Mapped[int] = mapped_column(primary_key=True)
    mac: Mapped[str] = mapped_column(String(32), unique=True)  # aa:bb:cc:dd:ee:ff
    hostname: Mapped[str] = mapped_column(String(190))
    image_id: Mapped[int | None] = mapped_column(ForeignKey("images.id", ondelete="SET NULL"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="ubuntu")
    # Ubuntu: sha-512 crypt hash. Windows: plaintext (autounattend needs it).
    password: Mapped[str] = mapped_column(String(255), default="")
    packages: Mapped[list] = mapped_column(JSON, default=list)
    enroll_on_install: Mapped[bool] = mapped_column(Boolean, default=True)
    # Per-machine config overrides on top of the image config: locale,
    # timezone, storage, kernel_args, post_script, plus optional static
    # network {ip, prefix, gateway, dns} (ubuntu).
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="new")  # new/rendered/installed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    image: Mapped[Image | None] = relationship()

    @property
    def mac_hyph(self) -> str:
        return self.mac.replace(":", "-").lower()
