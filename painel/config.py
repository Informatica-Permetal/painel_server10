"""Configuração lida de variáveis de ambiente.

Em produção o systemd carrega o arquivo privado /etc/painel-server10/painel.env
(EnvironmentFile, permissão 0600, fora do Git). Para desenvolvimento local,
defina PAINEL_ENV_FILE apontando para um arquivo .env.
"""
import os
from dataclasses import dataclass, field


def load_env_file(path):
    """Lê um arquivo KEY=VALUE simples; ausência ou falta de permissão retorna {}."""
    values = {}
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def _bool(value, default):
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "sim", "on")


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_baseline(text):
    """Converte SMART_BASELINE em {"smart.<id>.<contador>": valor}.

    Formato: "0:grown_defects=12,read_uncorrected=3;1:grown_defects=4".
    """
    seeds = {}
    for part in (text or "").split(";"):
        part = part.strip()
        if not part:
            continue
        disk, sep, items = part.partition(":")
        if not sep:
            raise ValueError(f"SMART_BASELINE inválido: {part!r}")
        disk_id = int(disk.strip())
        for item in items.split(","):
            item = item.strip()
            if not item:
                continue
            name, sep, value = item.partition("=")
            if not sep:
                raise ValueError(f"SMART_BASELINE inválido: {item!r}")
            seeds[f"smart.{disk_id}.{name.strip()}"] = int(value)
    return seeds


@dataclass
class Config:
    secret_key: str = ""
    admin_user: str = "admin"
    admin_password_hash: str = ""
    db_path: str = "/var/lib/painel-server10/painel.db"
    public_dashboard: bool = False
    cookie_secure: bool = True
    panel_url: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True
    smtp_ssl: bool = False

    megacli_path: str = "/opt/MegaRAID/MegaCli/MegaCli64"
    megacli_libdir: str = ""
    smartctl_path: str = "/usr/sbin/smartctl"
    smart_device: str = "/dev/sda"
    disk_ids: list = field(default_factory=lambda: [0, 1])
    ipmitool_path: str = "/usr/bin/ipmitool"
    smart_baseline: dict = field(default_factory=dict)

    raid_expected_vd: int = 1
    stale_minutes: int = 15
    fs_warn: int = 80
    fs_crit: int = 90
    ambient_warn: int = 35
    ambient_crit: int = 40
    fan_min_rpm: int = 1000
    command_timeout: int = 90
    retention_days: int = 7
    cpu_sample_seconds: float = 1.0

    @classmethod
    def from_env(cls, env):
        get = env.get
        disk_ids = [int(x) for x in get("SMART_DISK_IDS", "0,1").split(",") if x.strip()]
        return cls(
            secret_key=get("SECRET_KEY", ""),
            admin_user=get("ADMIN_USER", "admin"),
            admin_password_hash=get("ADMIN_PASSWORD_HASH", ""),
            db_path=get("DB_PATH", cls.db_path),
            public_dashboard=_bool(get("PUBLIC_DASHBOARD"), False),
            cookie_secure=_bool(get("SESSION_COOKIE_SECURE"), True),
            panel_url=get("PANEL_URL", ""),
            smtp_host=get("SMTP_HOST", ""),
            smtp_port=_int(get("SMTP_PORT"), 587),
            smtp_user=get("SMTP_USER", ""),
            smtp_password=get("SMTP_PASSWORD", ""),
            smtp_from=get("SMTP_FROM", ""),
            smtp_starttls=_bool(get("SMTP_STARTTLS"), True),
            smtp_ssl=_bool(get("SMTP_SSL"), False),
            megacli_path=get("MEGACLI_PATH", cls.megacli_path),
            megacli_libdir=get("MEGACLI_LIBDIR", ""),
            smartctl_path=get("SMARTCTL_PATH", cls.smartctl_path),
            smart_device=get("SMART_DEVICE", cls.smart_device),
            disk_ids=disk_ids,
            ipmitool_path=get("IPMITOOL_PATH", cls.ipmitool_path),
            smart_baseline=parse_baseline(get("SMART_BASELINE", "")),
            raid_expected_vd=_int(get("RAID_EXPECTED_VD"), 1),
            stale_minutes=_int(get("STALE_MINUTES"), 15),
            fs_warn=_int(get("FS_WARN_PERCENT"), 80),
            fs_crit=_int(get("FS_CRIT_PERCENT"), 90),
            ambient_warn=_int(get("AMBIENT_WARN_C"), 35),
            ambient_crit=_int(get("AMBIENT_CRIT_C"), 40),
            fan_min_rpm=_int(get("FAN_MIN_RPM"), 1000),
            command_timeout=_int(get("COMMAND_TIMEOUT"), 90),
            retention_days=_int(get("SNAPSHOT_RETENTION_DAYS"), 7),
        )


def load_config():
    env = dict(os.environ)
    env_file = env.get("PAINEL_ENV_FILE")
    if env_file:
        env = {**load_env_file(env_file), **env}
    return Config.from_env(env)
