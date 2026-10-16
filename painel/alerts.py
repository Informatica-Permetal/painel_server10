"""Detecção de transições, contadores cumulativos e notificações sem repetição.

- Cada verificação tem um estado persistido (check_state). Mudanças viram eventos.
- Contadores cumulativos (SMART/PERC) têm linha de base; só aumentos geram evento.
- E-mails saem por mudança de estado notificada, com intervalo mínimo
  (anti-oscilação) e lembrete opcional para problemas que persistem.
"""
from datetime import timedelta

from . import db, mailer
from .status import GROUP_TITLES, LABELS, MIN_SEVERITY_OPTIONS, OK, SEVERITY, worst

ONE_SHOT_MAX_AGE = timedelta(hours=24)


# ---------------------------------------------------------------- contadores

def update_counters(conn, observations, ts, seeds):
    """observations: lista de (chave, valor, ref, rótulo, grupo_disco).

    Retorna {chave: linha_de_base}. Gera eventos de aumento (notificáveis),
    de diminuição e de troca de disco (ref = número de série diferente).
    """
    baselines, replaced = {}, set()
    for key, value, ref, label, disk in observations:
        if value is None:
            continue
        row = conn.execute("SELECT * FROM counters WHERE key = ?", (key,)).fetchone()
        if row is None:
            base = seeds.get(key, value)
            conn.execute("INSERT INTO counters(key, baseline, last, ref, updated_at) VALUES(?, ?, ?, ?, ?)",
                         (key, base, base, ref, ts))
            row = {"baseline": base, "last": base, "ref": ref}
        baseline, last = row["baseline"], row["last"]

        if row["ref"] and ref and row["ref"] != ref:
            if disk not in replaced:
                replaced.add(disk)
                db.insert_event(conn, ts, "sistema", f"{disk}.troca", disk_label(disk), None, "ATENCAO",
                                f"Disco com novo número de série ({row['ref']} → {ref}); "
                                "linha de base reiniciada", notify=True)
            baseline = last = value
        elif value > last:
            db.insert_event(conn, ts, "contador", key, label, None, "ATENCAO",
                            f"{label} aumentou de {last} para {value} (linha de base {baseline})",
                            notify=True)
            last = value
        elif value < last:
            db.insert_event(conn, ts, "contador", key, label, None, OK,
                            f"{label} diminuiu de {last} para {value}; linha de base ajustada")
            baseline = min(baseline, value)
            last = value
        conn.execute("UPDATE counters SET baseline = ?, last = ?, ref = ?, updated_at = ? WHERE key = ?",
                     (baseline, last, ref or row["ref"], ts, key))
        baselines[key] = baseline
    return baselines


def disk_label(disk):
    return f"Disco ID {disk.split('.')[-1]}"


def accept_baseline(conn, disk_id, ts, user):
    """Administrador aceita os valores atuais do disco como nova linha de base."""
    cur = conn.execute("UPDATE counters SET baseline = last, updated_at = ? "
                       "WHERE key LIKE ? OR key LIKE ?", (ts, f"smart.{disk_id}.%", f"pd.{disk_id}.%"))
    if cur.rowcount:
        db.insert_event(conn, ts, "sistema", f"disco.{disk_id}.base", f"Disco ID {disk_id}", None, OK,
                        f"Linha de base dos contadores atualizada por {user}")
    return cur.rowcount


def update_boot(conn, boot_epoch, ts):
    """Registra reinício do servidor quando o horário de boot muda."""
    if boot_epoch is None:
        return
    boot = int(boot_epoch)
    row = conn.execute("SELECT last FROM counters WHERE key = 'system.boot'").fetchone()
    if row is None:
        conn.execute("INSERT INTO counters(key, baseline, last, updated_at) VALUES('system.boot', ?, ?, ?)",
                     (boot, boot, ts))
    elif abs(boot - row["last"]) > 180:
        db.insert_event(conn, ts, "sistema", "sistema.boot", "Servidor", None, "ATENCAO",
                        "Servidor foi reiniciado", notify=True)
        conn.execute("UPDATE counters SET last = ?, baseline = ?, updated_at = ? WHERE key = 'system.boot'",
                     (boot, boot, ts))


def sel_previous(conn):
    row = conn.execute("SELECT last FROM counters WHERE key = 'sel.entries'").fetchone()
    return None if row is None else row["last"]


def record_sel(conn, entries, new_lines, ts):
    """Atualiza a contagem do SEL. A primeira leitura é só linha de base."""
    prev = sel_previous(conn)
    if prev is None:
        conn.execute("INSERT INTO counters(key, baseline, last, updated_at) VALUES('sel.entries', ?, ?, ?)",
                     (entries, entries, ts))
        return
    if entries > prev:
        for line in new_lines:
            db.insert_event(conn, ts, "sel", "sel.novo", "Registro SEL", None, "ATENCAO",
                            f"Novo registro no SEL: {line}", notify=True)
        if not new_lines:
            db.insert_event(conn, ts, "sel", "sel.novo", "Registro SEL", None, "ATENCAO",
                            f"{entries - prev} novo(s) registro(s) no SEL", notify=True)
    elif entries < prev:
        db.insert_event(conn, ts, "sel", "sel.limpo", "Registro SEL", None, OK,
                        f"SEL foi esvaziado ({prev} → {entries} registros)")
    conn.execute("UPDATE counters SET last = ?, updated_at = ? WHERE key = 'sel.entries'", (entries, ts))


# ---------------------------------------------------------------- estados

def _full_label(check):
    return f"{GROUP_TITLES.get(check['group'], check['group'])} › {check['label']}"


def process_checks(conn, checks, ts, only_keys=None):
    """Atualiza check_state e grava eventos de transição.

    only_keys: limita o processamento (usado pelo watchdog), sem remover outros estados.
    """
    first_run = conn.execute("SELECT COUNT(*) FROM check_state").fetchone()[0] == 0 and only_keys is None
    present = set()
    for c in checks:
        if not c.get("alert", True) or c["status"] == "IGNORADO":
            continue
        present.add(c["key"])
        label = _full_label(c)
        detail = " — ".join(x for x in (c.get("value"), c.get("detail")) if x)
        row = conn.execute("SELECT * FROM check_state WHERE key = ?", (c["key"],)).fetchone()
        if row is None:
            conn.execute("INSERT INTO check_state(key, label, status, detail, since, updated_at) "
                         "VALUES(?, ?, ?, ?, ?, ?)", (c["key"], label, c["status"], detail, ts, ts))
            if c["status"] != OK:
                db.insert_event(conn, ts, "estado", c["key"], label, None, c["status"], detail)
        elif row["status"] != c["status"]:
            conn.execute("UPDATE check_state SET label = ?, status = ?, detail = ?, since = ?, updated_at = ? "
                         "WHERE key = ?", (label, c["status"], detail, ts, ts, c["key"]))
            db.insert_event(conn, ts, "estado", c["key"], label, row["status"], c["status"], detail)
        else:
            conn.execute("UPDATE check_state SET label = ?, detail = ?, updated_at = ? WHERE key = ?",
                         (label, detail, ts, c["key"]))

    if only_keys is None:
        for row in conn.execute("SELECT * FROM check_state").fetchall():
            if row["key"] in present:
                continue
            if row["status"] != OK:
                msg = "Item não aparece mais na coleta atual"
                conn.execute("UPDATE check_state SET status = ?, detail = ?, since = ?, updated_at = ? "
                             "WHERE key = ?", (OK, msg, ts, ts, row["key"]))
                db.insert_event(conn, ts, "estado", row["key"], row["label"], row["status"], OK, msg)
            elif (row["notified_status"] or OK) == OK:
                conn.execute("DELETE FROM check_state WHERE key = ?", (row["key"],))

    if first_run:
        db.insert_event(conn, ts, "sistema", None, "Monitoramento", None, OK,
                        "Monitoramento iniciado; contadores atuais registrados como linha de base")


# ---------------------------------------------------------------- notificação

def _pending(conn, settings, now):
    min_sev = MIN_SEVERITY_OPTIONS.get(settings["min_severity"], 1)
    cooldown = timedelta(minutes=int(settings["cooldown_minutes"]))
    reminder_hours = int(settings["reminder_hours"])
    items, silent = [], []
    for row in conn.execute("SELECT * FROM check_state ORDER BY key").fetchall():
        last = row["notified_status"] or OK
        status = row["status"]
        notified_at = db.parse_ts(row["notified_at"]) if row["notified_at"] else None
        if status == last:
            if (status != OK and reminder_hours > 0 and notified_at and SEVERITY[status] >= min_sev
                    and now - notified_at >= timedelta(hours=reminder_hours)):
                items.append((row, "lembrete"))
            continue
        if max(SEVERITY[status], SEVERITY[last]) < min_sev:
            silent.append(row)
            continue
        if notified_at and now - notified_at < cooldown:
            continue  # anti-oscilação: aguarda o intervalo mínimo
        items.append((row, "recuperado" if status == OK else "problema"))

    events, skipped = [], []
    limit = (now - ONE_SHOT_MAX_AGE).isoformat(timespec="seconds")
    for ev in conn.execute("SELECT * FROM events WHERE notify_pending = 1 ORDER BY id").fetchall():
        if ev["ts"] < limit or SEVERITY.get(ev["status"], 0) < min_sev:
            skipped.append(ev["id"])
        else:
            events.append(ev)
    return items, silent, events, skipped


def compose(items, events, hostname, panel_url, ts):
    statuses = [row["status"] for row, kind in items if kind != "recuperado"] + [e["status"] for e in events]
    headline = LABELS[worst(statuses)] if statuses else "Recuperação"
    count = len(items) + len(events)
    subject = f"[{hostname}] {headline}: {count} alerta(s) do Painel Server10"

    lines = [f"Painel Server10 — {hostname}", f"Verificação: {ts}", ""]
    sections = (("problema", "PROBLEMAS"), ("lembrete", "LEMBRETES (problema persistente)"),
                ("recuperado", "RECUPERADOS"))
    for kind, title in sections:
        rows = [row for row, k in items if k == kind]
        if rows:
            lines.append(title)
            for row in rows:
                lines.append(f"- [{LABELS[row['status']]}] {row['label']}: {row['detail']} "
                             f"(desde {row['since']})")
            lines.append("")
    if events:
        lines.append("EVENTOS")
        for ev in events:
            lines.append(f"- [{LABELS.get(ev['status'], ev['status'])}] {ev['ts']} {ev['message']}")
        lines.append("")
    if panel_url:
        lines.append(f"Painel: {panel_url}")
    lines.append("Mensagem automática; problemas persistentes não são reenviados a cada coleta.")
    return subject, "\n".join(lines)


def notify(conn, cfg, now, hostname, send=None):
    """Envia, se necessário, um único e-mail consolidado. Retorna um resumo."""
    send = send or mailer.send_mail
    settings = db.get_settings(conn)
    items, silent, events, skipped = _pending(conn, settings, now)
    ts = now.isoformat(timespec="seconds")

    for row in silent:
        conn.execute("UPDATE check_state SET notified_status = status WHERE key = ?", (row["key"],))
    for ev_id in skipped:
        conn.execute("UPDATE events SET notify_pending = 0 WHERE id = ?", (ev_id,))
    if not items and not events:
        return {"sent": False, "reason": "nada a notificar"}

    recipients = db.enabled_recipients(conn)
    reason = None
    if settings["alerts_enabled"] != "1":
        reason = "alertas desativados"
    elif not mailer.smtp_configured(cfg):
        reason = "e-mail não configurado"
    elif not recipients:
        reason = "nenhum destinatário ativo"
    if reason:
        # Eventos pontuais não acumulam; estados ficam pendentes até haver como enviar.
        for ev in events:
            conn.execute("UPDATE events SET notify_pending = 0 WHERE id = ?", (ev["id"],))
        return {"sent": False, "reason": reason}

    subject, body = compose(items, events, hostname, cfg.panel_url, ts)
    try:
        send(cfg, recipients, subject, body)
    except Exception as exc:  # noqa: BLE001 - qualquer falha de envio é registrada e repetida depois
        db.set_setting(conn, "last_mail_error", f"{ts} — {mailer.describe_error(exc)}")
        return {"sent": False, "reason": "falha no envio"}

    for row, _kind in items:
        conn.execute("UPDATE check_state SET notified_status = status, notified_at = ? WHERE key = ?",
                     (ts, row["key"]))
    for ev in events:
        conn.execute("UPDATE events SET notify_pending = 0, notified_at = ? WHERE id = ?", (ts, ev["id"]))
    db.set_setting(conn, "last_mail_ok", ts)
    db.set_setting(conn, "last_mail_error", "")
    return {"sent": True, "recipients": len(recipients), "items": len(items) + len(events),
            "subject": subject}

