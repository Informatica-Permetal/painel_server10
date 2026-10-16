"""Utilitários de teste: executor e /proc simulados a partir das fixtures.

Nenhum teste executa comandos reais nem acessa hardware.
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from painel import collector, db
from painel.config import Config

FIXTURES = Path(__file__).parent / "fixtures"
TZ = timezone(timedelta(hours=-3))
T0 = datetime(2026, 1, 15, 12, 0, tzinfo=TZ)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


DEFAULT_OUTPUTS = {
    "megacli_ld": "megacli_ldinfo.txt",
    "megacli_pd": "megacli_pdlist.txt",
    "megacli_bbu": "megacli_bbu.txt",
    "ipmi_sdr": "ipmi_sdr_elist.txt",
    "ipmi_chassis": "ipmi_chassis.txt",
    "ipmi_sel_info": "ipmi_sel_info.txt",
    "smart_0": "smartctl_0.txt",
    "smart_1": "smartctl_1.txt",
}


class FakeRunner:
    """Devolve fixtures por nome de comando; permite substituir ou simular falha."""

    def __init__(self):
        self.outputs = {name: fixture(f) for name, f in DEFAULT_OUTPUTS.items()}
        self.calls = []

    def __call__(self, name, argv):
        self.calls.append((name, list(argv)))
        text = self.outputs.get(name)
        if text is None:
            return collector.CommandResult(False, "", "comando não encontrado")
        return collector.CommandResult(True, text)

    def replace(self, name, old, new):
        assert old in self.outputs[name], f"{old!r} não está em {name}"
        self.outputs[name] = self.outputs[name].replace(old, new)


def fake_system(uptime=86400.0 * 3):
    return {
        "cpu": {"percent": 12.5, "ncpu": 16, "load": [0.3, 0.4, 0.5]},
        "mem": {"MemTotal": 32901232, "MemAvailable": 30000000, "SwapTotal": 8388604, "SwapFree": 8388604},
        "uptime": uptime,
        "filesystems": [{"mount": "/", "size": 100 * 2**30, "used": 20 * 2**30, "avail": 75 * 2**30, "pct": 21.0}],
    }


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=str(tmp_path / "painel.db"), secret_key="teste", cookie_secure=False,
                  smtp_host="smtp.exemplo.invalid", smtp_from="painel@exemplo.invalid")


@pytest.fixture
def conn(cfg):
    connection = db.connect(cfg.db_path)
    yield connection
    connection.close()


@pytest.fixture
def runner():
    return FakeRunner()


class Outbox:
    def __init__(self):
        self.sent = []
        self.fail = False

    def __call__(self, cfg, recipients, subject, body):
        if self.fail:
            raise ConnectionRefusedError("servidor indisponível")
        self.sent.append(SimpleNamespace(recipients=list(recipients), subject=subject, body=body))


@pytest.fixture
def outbox():
    return Outbox()


def run_once(conn, cfg, runner, now, system=None):
    # uptime acompanha o relógio simulado, para não parecer reinício a cada coleta
    system = system or fake_system(uptime=86400.0 * 3 + (now - T0).total_seconds())
    snap = collector.collect(conn, cfg, runner, system, now, "server-teste")
    conn.commit()
    return snap


def by_key(snapshot):
    return {c["key"]: c for c in snapshot["checks"]}


os.environ.pop("PAINEL_ENV_FILE", None)
