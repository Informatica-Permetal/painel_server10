"""Cenários de coleta, linha de base, falha/recuperação e antirrepetição de e-mails."""
from datetime import timedelta

from painel import alerts, collector, db
from painel.status import ATENCAO, CRITICO, DESCONHECIDO, IGNORADO, OK

from .conftest import T0, by_key, fake_system, run_once


def events(conn, kind=None):
    rows = db.recent_events(conn, 1000)
    return [r for r in rows if kind is None or r["kind"] == kind]


def test_coleta_normal_tudo_ok(conn, cfg, runner):
    snap = run_once(conn, cfg, runner, T0)
    checks = by_key(snap)
    assert snap["overall"] == OK
    assert checks["raid.vd0"]["status"] == OK
    assert checks["disco.0.estado"]["status"] == OK
    assert checks["disco.1.smart"]["status"] == OK
    assert checks["bbu.estado"]["status"] == OK
    assert checks["chassis.falhas"]["status"] == OK
    # FAN 5 e sensores "ns/Disabled" aparecem como desativados e não alteram o estado geral
    fan5 = next(c for c in snap["checks"] if c["label"] == "FAN MOD 5A RPM")
    assert fan5["status"] == IGNORADO
    # Fontes redundantes
    assert any(c["label"] == "Fonte 2 · Status" and c["status"] == OK for c in snap["checks"])
    # Primeira coleta: só o evento de início; contadores antigos viram linha de base
    assert [e["message"][:25] for e in events(conn)] == ["Monitoramento iniciado; c"]


def test_linha_base_configurada_e_aumento_de_contador(conn, cfg, runner, outbox):
    cfg.smart_baseline = {"smart.0.grown_defects": 9, "smart.0.read_uncorrected": 1}
    db.add_recipient(conn, "ti@exemplo.invalid")
    run_once(conn, cfg, runner, T0)
    assert alerts.notify(conn, cfg, T0, "server-teste", send=outbox)["sent"] is False
    assert outbox.sent == []

    runner.replace("smart_0", "Elements in grown defect list: 9", "Elements in grown defect list: 10")
    t1 = T0 + timedelta(minutes=5)
    snap = run_once(conn, cfg, runner, t1)
    assert by_key(snap)["disco.0.smart"]["status"] == ATENCAO
    counter_events = events(conn, "contador")
    assert len(counter_events) == 1
    assert "aumentou de 9 para 10" in counter_events[0]["message"]

    result = alerts.notify(conn, cfg, t1, "server-teste", send=outbox)
    assert result["sent"] is True
    assert "aumentou de 9 para 10" in outbox.sent[0].body
    assert outbox.sent[0].recipients == ["ti@exemplo.invalid"]

    # Coletas seguintes com o mesmo valor: nenhum alerta novo
    for i in range(2, 6):
        t = T0 + timedelta(minutes=5 * i)
        run_once(conn, cfg, runner, t)
        alerts.notify(conn, cfg, t, "server-teste", send=outbox)
    assert len(outbox.sent) == 1
    assert len(events(conn, "contador")) == 1

    # Administrador aceita a nova linha de base -> disco volta a OK
    alerts.accept_baseline(conn, 0, (T0 + timedelta(minutes=40)).isoformat(), "admin")
    snap = run_once(conn, cfg, runner, T0 + timedelta(minutes=45))
    assert by_key(snap)["disco.0.smart"]["status"] == OK


def test_baseline_sem_semente_nao_alerta_contadores_antigos(conn, cfg, runner):
    snap = run_once(conn, cfg, runner, T0)
    assert by_key(snap)["disco.1.smart"]["status"] == OK
    assert "Erros de verificação não corrigidos: 3 (base 3)" in by_key(snap)["disco.1.smart"]["detail"]
    assert events(conn, "contador") == []


def test_raid_degradado_e_recuperacao_com_antirrepeticao(conn, cfg, runner, outbox):
    db.add_recipient(conn, "a@exemplo.invalid")
    db.add_recipient(conn, "b@exemplo.invalid")
    run_once(conn, cfg, runner, T0)
    alerts.notify(conn, cfg, T0, "server-teste", send=outbox)

    runner.replace("megacli_ld", "State               : Optimal", "State               : Degraded")
    runner.replace("megacli_pd", "Online, Spun Up\nDevice Firmware Level: 0008\nInquiry Data: SEAGATE ST3300657SS     0008TESTSER1",
                   "Failed\nDevice Firmware Level: 0008\nInquiry Data: SEAGATE ST3300657SS     0008TESTSER1")
    t1 = T0 + timedelta(minutes=5)
    snap = run_once(conn, cfg, runner, t1)
    checks = by_key(snap)
    assert checks["raid.vd0"]["status"] == CRITICO
    assert checks["disco.1.estado"]["status"] == CRITICO
    assert snap["overall"] == CRITICO
    alerts.notify(conn, cfg, t1, "server-teste", send=outbox)
    assert len(outbox.sent) == 1
    assert "PROBLEMAS" in outbox.sent[0].body and "Degraded" in outbox.sent[0].body
    assert sorted(outbox.sent[0].recipients) == ["a@exemplo.invalid", "b@exemplo.invalid"]

    # Problema persiste: sem novo e-mail a cada 5 minutos
    for i in range(2, 8):
        t = T0 + timedelta(minutes=5 * i)
        run_once(conn, cfg, runner, t)
        alerts.notify(conn, cfg, t, "server-teste", send=outbox)
    assert len(outbox.sent) == 1

    # Recuperação gera um e-mail de recuperação
    runner.replace("megacli_ld", "Degraded", "Optimal")
    runner.replace("megacli_pd", "Failed", "Online, Spun Up")
    t9 = T0 + timedelta(minutes=45)
    snap = run_once(conn, cfg, runner, t9)
    assert snap["overall"] == OK
    alerts.notify(conn, cfg, t9, "server-teste", send=outbox)
    assert len(outbox.sent) == 2
    assert "RECUPERADOS" in outbox.sent[1].body

    # Lembrete de problema persistente só após o intervalo configurado (24 h)
    transitions = [e for e in events(conn, "estado") if e["key"] == "raid.vd0"]
    assert [(e["prev_status"], e["status"]) for e in reversed(transitions)] == [(OK, CRITICO), (CRITICO, OK)]


def test_lembrete_apos_24h(conn, cfg, runner, outbox):
    db.add_recipient(conn, "a@exemplo.invalid")
    runner.replace("megacli_bbu", "Battery Replacement required            : No",
                   "Battery Replacement required            : Yes")
    run_once(conn, cfg, runner, T0)
    alerts.notify(conn, cfg, T0, "server-teste", send=outbox)
    assert len(outbox.sent) == 1
    t = T0 + timedelta(hours=23)
    run_once(conn, cfg, runner, t)
    alerts.notify(conn, cfg, t, "server-teste", send=outbox)
    assert len(outbox.sent) == 1
    t = T0 + timedelta(hours=24, minutes=5)
    run_once(conn, cfg, runner, t)
    alerts.notify(conn, cfg, t, "server-teste", send=outbox)
    assert len(outbox.sent) == 2
    assert "LEMBRETES" in outbox.sent[1].body


def test_falha_de_comando_vira_desconhecido(conn, cfg, runner, outbox):
    db.add_recipient(conn, "a@exemplo.invalid")
    del runner.outputs["megacli_ld"]
    del runner.outputs["megacli_pd"]
    del runner.outputs["megacli_bbu"]
    runner.outputs["smart_1"] = "Smartctl open device: /dev/sda [megaraid_disk_01] failed: INQUIRY failed"
    runner.outputs["ipmi_sdr"] = "Could not open device at /dev/ipmi0"
    snap = run_once(conn, cfg, runner, T0)
    checks = by_key(snap)
    assert checks["raid.coleta"]["status"] == DESCONHECIDO
    assert checks["disco.0.estado"]["status"] == DESCONHECIDO
    assert checks["disco.1.smart"]["status"] == DESCONHECIDO
    assert checks["bbu.estado"]["status"] == DESCONHECIDO
    assert checks["ventilacao.coleta"]["status"] == DESCONHECIDO
    assert checks["coleta.comandos"]["status"] == DESCONHECIDO
    assert snap["overall"] == DESCONHECIDO  # nunca OK quando a coleta falha
    alerts.notify(conn, cfg, T0, "server-teste", send=outbox)
    assert len(outbox.sent) == 1


def test_fonte_com_falha_e_redundancia_perdida(conn, cfg, runner):
    runner.replace("ipmi_sdr", "Status           | 86h | ok  | 10.2 | Presence detected",
                   "Status           | 86h | ok  | 10.2 | Presence detected, Power Supply AC lost")
    runner.replace("ipmi_sdr", "PS Redundancy    | 77h | ok  |  7.1 | Fully Redundant",
                   "PS Redundancy    | 77h | cr  |  7.1 | Redundancy Lost")
    snap = run_once(conn, cfg, runner, T0)
    energia = {c["label"]: c["status"] for c in snap["checks"] if c["group"] == "energia"}
    assert energia["Fonte 2 · Status"] == CRITICO
    assert energia["PS Redundancy"] == CRITICO


def test_ventoinha_e_temperatura(conn, cfg, runner):
    runner.replace("ipmi_sdr", "FAN MOD 2A RPM   | 31h | ok  |  7.1 | 3480 RPM",
                   "FAN MOD 2A RPM   | 31h | cr  |  7.1 | 0 RPM")
    runner.replace("ipmi_sdr", "19 degrees C", "37 degrees C")
    snap = run_once(conn, cfg, runner, T0)
    status = {c["label"]: c["status"] for c in snap["checks"]}
    assert status["FAN MOD 2A RPM"] == CRITICO
    assert status["Ambient Temp"] == ATENCAO


def test_novo_registro_sel_gera_evento_unico(conn, cfg, runner):
    run_once(conn, cfg, runner, T0)
    runner.replace("ipmi_sel_info", "Entries          : 1", "Entries          : 2")
    runner.outputs["ipmi_sel_tail"] = "   2 | 10/16/2026 | 12:04:00 | Power Supply #0x86 | Power Supply AC lost | Asserted\n"
    run_once(conn, cfg, runner, T0 + timedelta(minutes=5))
    run_once(conn, cfg, runner, T0 + timedelta(minutes=10))
    sel_events = events(conn, "sel")
    assert len(sel_events) == 1
    assert "Power Supply AC lost" in sel_events[0]["message"]
    tail_calls = [argv for name, argv in runner.calls if name == "ipmi_sel_tail"]
    assert tail_calls == [[cfg.ipmitool_path, "sel", "elist", "last", "1"]]


def test_troca_de_disco_reinicia_linha_de_base(conn, cfg, runner):
    run_once(conn, cfg, runner, T0)
    runner.replace("smart_0", "TESTSER0", "NOVOSER9")
    runner.replace("smart_0", "Elements in grown defect list: 9", "Elements in grown defect list: 0")
    snap = run_once(conn, cfg, runner, T0 + timedelta(minutes=5))
    assert by_key(snap)["disco.0.smart"]["status"] == OK
    assert any("novo número de série" in e["message"] for e in events(conn, "sistema"))


def test_reinicio_do_servidor_gera_evento(conn, cfg, runner):
    run_once(conn, cfg, runner, T0, fake_system(uptime=86400))
    run_once(conn, cfg, runner, T0 + timedelta(minutes=5), fake_system(uptime=120))
    assert any(e["message"] == "Servidor foi reiniciado" for e in events(conn, "sistema"))


def test_watchdog_coleta_parada(conn, cfg, runner, outbox):
    db.add_recipient(conn, "a@exemplo.invalid")
    run_once(conn, cfg, runner, T0)
    assert collector.watchdog(conn, cfg, T0 + timedelta(minutes=10)) is False
    later = T0 + timedelta(minutes=30)
    assert collector.watchdog(conn, cfg, later) is True
    alerts.notify(conn, cfg, later, "server-teste", send=outbox)
    assert len(outbox.sent) == 1 and "Sem coleta há 30 min" in outbox.sent[0].body
    # Coleta volta: a recuperação respeita o intervalo mínimo (30 min) antes de ser enviada
    t = later + timedelta(minutes=5)
    run_once(conn, cfg, runner, t)
    alerts.notify(conn, cfg, t, "server-teste", send=outbox)
    assert len(outbox.sent) == 1
    t = later + timedelta(minutes=30)
    run_once(conn, cfg, runner, t)
    alerts.notify(conn, cfg, t, "server-teste", send=outbox)
    assert len(outbox.sent) == 2 and "RECUPERADOS" in outbox.sent[1].body


def test_sem_smtp_ou_destinatarios_nao_envia(conn, cfg, runner, outbox):
    runner.replace("megacli_ld", "Optimal", "Degraded")
    run_once(conn, cfg, runner, T0)
    assert alerts.notify(conn, cfg, T0, "server-teste", send=outbox)["reason"] == "nenhum destinatário ativo"
    db.add_recipient(conn, "a@exemplo.invalid")
    cfg.smtp_host = ""
    assert alerts.notify(conn, cfg, T0, "server-teste", send=outbox)["reason"] == "e-mail não configurado"
    assert outbox.sent == []
    # Ao configurar o SMTP, o problema atual (ainda não notificado) é enviado uma vez
    cfg.smtp_host = "smtp.exemplo.invalid"
    assert alerts.notify(conn, cfg, T0, "server-teste", send=outbox)["sent"] is True


def test_falha_de_envio_e_repetida_e_registrada(conn, cfg, runner, outbox):
    db.add_recipient(conn, "a@exemplo.invalid")
    runner.replace("megacli_ld", "Optimal", "Degraded")
    run_once(conn, cfg, runner, T0)
    outbox.fail = True
    assert alerts.notify(conn, cfg, T0, "server-teste", send=outbox)["reason"] == "falha no envio"
    assert "servidor indisponível" in db.get_settings(conn)["last_mail_error"]
    outbox.fail = False
    assert alerts.notify(conn, cfg, T0 + timedelta(minutes=5), "server-teste", send=outbox)["sent"] is True


def test_somente_criticos(conn, cfg, runner, outbox):
    db.add_recipient(conn, "a@exemplo.invalid")
    db.set_setting(conn, "min_severity", "CRITICO")
    runner.replace("ipmi_sdr", "19 degrees C", "37 degrees C")  # apenas atenção
    run_once(conn, cfg, runner, T0)
    assert alerts.notify(conn, cfg, T0, "server-teste", send=outbox)["sent"] is False
    assert outbox.sent == []


def test_comandos_sao_fixos_e_somente_leitura(cfg):
    commands = collector.build_commands(cfg)
    flat = " ".join(" ".join(argv) for argv in commands.values()).lower()
    for forbidden in ("clear", "-set", "delete", "-start", "-t long", "-x", "rebuild", "chassis power"):
        assert forbidden not in flat
    assert commands["smart_1"] == [cfg.smartctl_path, "-a", "-d", "megaraid,1", cfg.smart_device]


def test_executor_real_trata_comando_inexistente(cfg, tmp_path):
    cfg.command_timeout = 5
    run = collector.make_runner(cfg, str(tmp_path))
    result = run("megacli_ld", [str(tmp_path / "nao-existe"), "-LDInfo"])
    assert result.ok is False and result.error == "comando não encontrado"
