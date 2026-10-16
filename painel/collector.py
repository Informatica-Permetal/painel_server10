"""Coletor periódico (executado pelo systemd timer, como root).

Executa SOMENTE comandos fixos de leitura, sem shell, com timeout. Nenhum
argumento vem da interface web. Falha de comando = estado DESCONHECIDO.

Uso:
    python -m painel.collector              # uma coleta completa
    python -m painel.collector --watchdog   # só verifica se a coleta parou
"""
import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime

from . import alerts, checks, db, parsers
from .config import load_config
from .status import DESCONHECIDO, OK, worst


@dataclass
class CommandResult:
    ok: bool
    output: str
    error: str = ""
    seconds: float = 0.0


def build_commands(cfg):
    mega = cfg.megacli_path
    ipmi = cfg.ipmitool_path
    commands = {
        "megacli_ld": [mega, "-LDInfo", "-Lall", "-aALL", "-NoLog"],
        "megacli_pd": [mega, "-PDList", "-aALL", "-NoLog"],
        "megacli_bbu": [mega, "-AdpBbuCmd", "-GetBbuStatus", "-aALL", "-NoLog"],
        "ipmi_sdr": [ipmi, "sdr", "elist"],
        "ipmi_chassis": [ipmi, "chassis", "status"],
        "ipmi_sel_info": [ipmi, "sel", "info"],
    }
    for disk_id in cfg.disk_ids:
        commands[f"smart_{disk_id}"] = [cfg.smartctl_path, "-a", "-d", f"megaraid,{int(disk_id)}",
                                        cfg.smart_device]
    return commands


def sel_tail_command(cfg, count):
    return [cfg.ipmitool_path, "sel", "elist", "last", str(max(1, min(int(count), 20)))]


def make_runner(cfg, workdir):
    """Executor real. MegaCLI roda num diretório temporário (gera arquivos de log)."""
    def run(name, argv):
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
        if name.startswith("megacli") and cfg.megacli_libdir:
            env["LD_LIBRARY_PATH"] = cfg.megacli_libdir
        start = time.monotonic()
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                                  timeout=cfg.command_timeout, env=env, cwd=workdir,
                                  stdin=subprocess.DEVNULL, check=False)
        except FileNotFoundError:
            return CommandResult(False, "", "comando não encontrado", time.monotonic() - start)
        except subprocess.TimeoutExpired:
            return CommandResult(False, "", f"tempo limite de {cfg.command_timeout}s excedido",
                                 time.monotonic() - start)
        except OSError as exc:
            return CommandResult(False, "", f"erro ao executar: {exc.strerror}", time.monotonic() - start)
        elapsed = time.monotonic() - start
        if proc.stdout.strip():
            return CommandResult(True, proc.stdout, "", elapsed)
        error = (proc.stderr or "").strip().splitlines()
        return CommandResult(False, "", (error[-1] if error else f"sem saída (código {proc.returncode})")[:200],
                             elapsed)
    return run


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def read_system(read=read_text, statvfs=None, sleep=time.sleep, sample=1.0):
    statvfs = statvfs or os.statvfs
    info = {}
    try:
        first = parsers.parse_cpu_stat(read("/proc/stat"))
        sleep(sample)
        second = parsers.parse_cpu_stat(read("/proc/stat"))
        info["cpu"] = {"percent": parsers.cpu_percent(first, second),
                       "ncpu": second[2] or os.cpu_count() or 1,
                       "load": parsers.parse_loadavg(read("/proc/loadavg"))}
    except (OSError, ValueError, IndexError) as exc:
        info["cpu_error"] = str(exc)
    try:
        info["mem"] = parsers.parse_meminfo(read("/proc/meminfo"))
    except OSError as exc:
        info["mem_error"] = str(exc)
    try:
        info["uptime"] = parsers.parse_uptime(read("/proc/uptime"))
    except (OSError, ValueError, IndexError) as exc:
        info["uptime_error"] = str(exc)
    try:
        filesystems = []
        for mount in parsers.parse_mounts(read("/proc/mounts")):
            try:
                st = statvfs(mount)
            except OSError as exc:
                filesystems.append({"mount": mount, "error": str(exc)})
                continue
            size = st.f_blocks * st.f_frsize
            used = (st.f_blocks - st.f_bfree) * st.f_frsize
            avail = st.f_bavail * st.f_frsize
            pct = 100.0 * used / (used + avail) if used + avail else 0.0
            filesystems.append({"mount": mount, "size": size, "used": used, "avail": avail, "pct": pct})
        info["filesystems"] = filesystems
    except OSError as exc:
        info["fs_error"] = str(exc)
    return info


def _parse(results, name, parser):
    res = results.get(name)
    if not res or not res.ok:
        return None, (res.error if res else "não executado")
    try:
        value = parser(res.output)
    except (ValueError, IndexError, KeyError) as exc:
        return None, f"saída inesperada: {exc}"
    if value is None or value == []:
        return None, "saída sem os dados esperados"
    return value, ""


def collect(conn, cfg, run, system, now, hostname):
    """Executa os comandos, atualiza contadores/estados e grava a coleta. Retorna o snapshot."""
    ts = now.isoformat(timespec="seconds")
    started = time.monotonic()
    commands = build_commands(cfg)
    results = {name: run(name, argv) for name, argv in commands.items()}

    vds, vd_err = _parse(results, "megacli_ld", parsers.parse_megacli_ldinfo)
    pds, pd_err = _parse(results, "megacli_pd", parsers.parse_megacli_pdlist)
    bbu, bbu_err = _parse(results, "megacli_bbu", parsers.parse_megacli_bbu)
    sensors, sdr_err = _parse(results, "ipmi_sdr", parsers.parse_ipmi_sdr_elist)
    chassis, chassis_err = _parse(results, "ipmi_chassis", parsers.parse_kv)
    sel, sel_err = _parse(results, "ipmi_sel_info", parsers.parse_ipmi_sel_info)
    smart = {}
    for disk_id in cfg.disk_ids:
        smart[disk_id], _ = _parse(results, f"smart_{disk_id}", parsers.parse_smartctl)

    # Contadores cumulativos: compara com a linha de base, não com zero.
    observations = []
    for disk_id, data in smart.items():
        for name, text in checks.SMART_COUNTERS.items():
            if data and name in data:
                observations.append((f"smart.{disk_id}.{name}", data[name], data.get("serial"),
                                     f"Disco ID {disk_id}: {text}", f"smart.{disk_id}"))
    for pd in pds or []:
        if pd["device_id"] not in cfg.disk_ids:
            continue
        for name, text in checks.PD_COUNTERS.items():
            observations.append((f"pd.{pd['device_id']}.{name}", pd.get(name), pd.get("inquiry"),
                                 f"Disco ID {pd['device_id']}: {text}", f"pd.{pd['device_id']}"))
    baselines = alerts.update_counters(conn, observations, ts, cfg.smart_baseline)

    if sel and sel.get("entries") is not None:
        prev = alerts.sel_previous(conn)
        new_lines = []
        if prev is not None and sel["entries"] > prev:
            res = run("ipmi_sel_tail", sel_tail_command(cfg, sel["entries"] - prev))
            if res.ok:
                new_lines = parsers.parse_sel_lines(res.output)
            results["ipmi_sel_tail"] = res
        alerts.record_sel(conn, sel["entries"], new_lines, ts)

    if system.get("uptime") is not None:
        alerts.update_boot(conn, now.timestamp() - system["uptime"], ts)

    items = []
    items += checks.system_checks(system, cfg)
    items += checks.raid_checks(vds, cfg, vd_err)
    items += checks.disk_checks(pds, smart, baselines, cfg, pd_err)
    items += checks.bbu_checks(bbu, bbu_err)
    items += checks.ipmi_checks(sensors, cfg, sdr_err)
    items += checks.chassis_checks(chassis, sel, chassis_err, sel_err)

    failed = [name for name, res in results.items() if not res.ok]
    elapsed = time.monotonic() - started
    items.append(checks.check("coleta.comandos", "coleta", "Comandos de leitura",
                              DESCONHECIDO if failed else OK,
                              f"{len(results) - len(failed)} de {len(results)} com resposta",
                              ("Sem resposta: " + ", ".join(failed)) if failed else f"Duração {checks.num(elapsed)} s"))
    items.append(checks.check("coleta.atraso", "coleta", "Coleta periódica", OK, "Em dia",
                              f"Considerada parada após {cfg.stale_minutes} min sem coleta"))

    alerts.process_checks(conn, items, ts)
    overall = worst(c["status"] for c in items)
    snapshot = {
        "ts": ts,
        "hostname": hostname,
        "overall": overall,
        "duration_s": round(elapsed, 1),
        "checks": items,
        "commands": {name: {"ok": res.ok, "error": res.error, "seconds": round(res.seconds, 1)}
                     for name, res in results.items()},
    }
    db.save_snapshot(conn, ts, overall, snapshot)
    db.prune(conn, cfg.retention_days, now)
    return snapshot


def watchdog(conn, cfg, now):
    """Marca a coleta como parada quando a última é antiga demais."""
    snap = db.latest_snapshot(conn)
    ts = now.isoformat(timespec="seconds")
    if snap is None:
        return False
    age = (now - db.parse_ts(snap["ts"])).total_seconds() / 60
    if age <= cfg.stale_minutes:
        return False
    item = checks.check("coleta.atraso", "coleta", "Coleta periódica", DESCONHECIDO,
                        f"Sem coleta há {int(age)} min", f"Última coleta: {snap['ts']}")
    alerts.process_checks(conn, [item], ts, only_keys={"coleta.atraso"})
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="Coletor do Painel Server10")
    parser.add_argument("--watchdog", action="store_true", help="apenas verifica coleta parada")
    parser.add_argument("--no-email", action="store_true", help="não envia e-mail nesta execução")
    args = parser.parse_args(argv)

    cfg = load_config()
    now = datetime.now().astimezone()
    conn = db.connect(cfg.db_path)
    try:
        if args.watchdog:
            stale = watchdog(conn, cfg, now)
            snap = db.latest_snapshot(conn)
            hostname = snap["hostname"] if snap else socket.gethostname()
            print("coleta parada" if stale else "coleta em dia")
        else:
            with tempfile.TemporaryDirectory(prefix="painel-") as workdir:
                system = read_system(sample=cfg.cpu_sample_seconds)
                snapshot = collect(conn, cfg, make_runner(cfg, workdir), system, now, socket.gethostname())
            hostname = snapshot["hostname"]
            print(f"coleta {snapshot['ts']}: {snapshot['overall']} em {snapshot['duration_s']} s")
        conn.commit()
        if not args.no_email:
            result = alerts.notify(conn, cfg, now, hostname)
            conn.commit()
            if result["sent"]:
                print(f"e-mail enviado: {result['subject']}")
            elif result["reason"] != "nada a notificar":
                print(f"e-mail não enviado: {result['reason']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
