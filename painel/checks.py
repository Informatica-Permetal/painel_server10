"""Regras de avaliação: transformam dados lidos em verificações com estado.

Cada verificação é um dict com: key, group, label, status, value, detail,
pct (opcional, para barra de uso) e alert (False = apenas informativo).
"""
import re

from .status import ATENCAO, CRITICO, DESCONHECIDO, IGNORADO, OK, worst

SMART_COUNTERS = {
    "grown_defects": "Defeitos acumulados",
    "read_uncorrected": "Erros de leitura não corrigidos",
    "write_uncorrected": "Erros de escrita não corrigidos",
    "verify_uncorrected": "Erros de verificação não corrigidos",
    "non_medium": "Non-medium errors",
}
PD_COUNTERS = {
    "media_errors": "Erros de mídia (PERC)",
    "predictive_failures": "Falhas preditivas (PERC)",
}


def check(key, group, label, status, value="", detail="", pct=None, alert=True):
    return {"key": key, "group": group, "label": label, "status": status,
            "value": value, "detail": detail, "pct": pct, "alert": alert}


def unknown(key, group, label, reason):
    return check(key, group, label, DESCONHECIDO, "Sem leitura", reason)


def num(value, digits=1):
    return f"{value:.{digits}f}".replace(".", ",")


def fmt_bytes(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{num(n, 0 if unit == 'B' else 1)} {unit}"
        n /= 1024.0


def fmt_duration(seconds):
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} d {hours} h {minutes} min"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def by_threshold(value, warn, crit):
    if value is None:
        return DESCONHECIDO
    if value >= crit:
        return CRITICO
    if value >= warn:
        return ATENCAO
    return OK


# ---------------------------------------------------------------- sistema

def system_checks(sysinfo, cfg):
    out = []
    g = "sistema"

    cpu = sysinfo.get("cpu")
    if cpu:
        ncpu = max(cpu["ncpu"], 1)
        load = cpu["load"]
        ratio = load[2] / ncpu
        status = CRITICO if ratio > 2.0 else ATENCAO if ratio > 1.0 else OK
        pct = cpu["percent"]
        value = (f"{num(pct, 0)}% em uso · " if pct is not None else "") + \
            f"carga {num(load[0], 2)} / {num(load[1], 2)} / {num(load[2], 2)}"
        out.append(check("sistema.cpu", g, "CPU", status, value,
                         f"{ncpu} núcleos lógicos; carga média de 1, 5 e 15 min", pct))
    else:
        out.append(unknown("sistema.cpu", g, "CPU", sysinfo.get("cpu_error", "")))

    mem = sysinfo.get("mem")
    if mem and mem.get("MemTotal"):
        total = mem["MemTotal"] * 1024
        avail = mem.get("MemAvailable", mem.get("MemFree", 0)) * 1024
        pct = 100.0 * (total - avail) / total
        out.append(check("sistema.ram", g, "Memória RAM", by_threshold(pct, 90, 95),
                         f"{fmt_bytes(total - avail)} de {fmt_bytes(total)} ({num(pct, 0)}%)",
                         f"Disponível: {fmt_bytes(avail)}", pct))
        swap_total = mem.get("SwapTotal", 0) * 1024
        if swap_total:
            swap_used = swap_total - mem.get("SwapFree", 0) * 1024
            spct = 100.0 * swap_used / swap_total
            out.append(check("sistema.swap", g, "Swap", by_threshold(spct, 60, 85),
                             f"{fmt_bytes(swap_used)} de {fmt_bytes(swap_total)} ({num(spct, 0)}%)",
                             pct=spct))
        else:
            out.append(check("sistema.swap", g, "Swap", OK, "Sem swap configurada"))
    else:
        err = sysinfo.get("mem_error", "")
        out.append(unknown("sistema.ram", g, "Memória RAM", err))
        out.append(unknown("sistema.swap", g, "Swap", err))

    uptime = sysinfo.get("uptime")
    if uptime is not None:
        out.append(check("sistema.uptime", g, "Tempo ligado", OK, fmt_duration(uptime), alert=False))
    else:
        out.append(unknown("sistema.uptime", g, "Tempo ligado", sysinfo.get("uptime_error", "")))

    fs_list = sysinfo.get("filesystems")
    if fs_list is None:
        out.append(unknown("fs.leitura", "armazenamento", "Volumes", sysinfo.get("fs_error", "")))
    for fs in fs_list or []:
        key = f"fs.{fs['mount']}"
        if fs.get("error"):
            out.append(unknown(key, "armazenamento", fs["mount"], fs["error"]))
            continue
        pct = fs["pct"]
        out.append(check(key, "armazenamento", fs["mount"], by_threshold(pct, cfg.fs_warn, cfg.fs_crit),
                         f"{fmt_bytes(fs['used'])} de {fmt_bytes(fs['size'])} ({num(pct, 0)}%)",
                         f"Livre: {fmt_bytes(fs['avail'])}", pct))
    return out


# ---------------------------------------------------------------- RAID

def _vd_status(state):
    s = state.lower()
    if s == "optimal":
        return OK
    if "degraded" in s or "offline" in s or "failed" in s:
        return CRITICO
    return ATENCAO


def raid_checks(vds, cfg, error=""):
    g = "raid"
    if vds is None:
        return [unknown("raid.coleta", g, "Controladora", error or "MegaCLI sem resposta válida")]
    out = []
    for vd in vds:
        name = vd.get("name") or f"VD {vd['id']}"
        value = " · ".join(x for x in (vd.get("raid_level"), vd.get("size"), vd["state"]) if x)
        detail = f"{vd['drives']} discos" if vd.get("drives") is not None else ""
        out.append(check(f"raid.vd{vd['id']}", g, f"Volume {name}", _vd_status(vd["state"]), value, detail))
    if len(vds) < cfg.raid_expected_vd:
        out.append(check("raid.ausente", g, "Volumes esperados", CRITICO,
                         f"{len(vds)} de {cfg.raid_expected_vd}", "Volume virtual não encontrado"))
    return out


# ---------------------------------------------------------------- discos

def _pd_status(pd):
    s = pd.get("state", "").lower()
    if pd.get("smart_alert"):
        return CRITICO
    if not s:
        return DESCONHECIDO
    if any(w in s for w in ("failed", "offline", "bad", "missing")):
        return CRITICO
    if s.startswith("online") or "hotspare" in s:
        return OK
    return ATENCAO  # rebuild, copyback, unconfigured(good) etc.


def disk_checks(pds, smart, baselines, cfg, pd_error=""):
    """pds: lista do MegaCLI (None se falhou); smart: {id: dict|None}; baselines: {chave: valor}."""
    g = "discos"
    out = []
    for disk_id in cfg.disk_ids:
        pd = None if pds is None else next((p for p in pds if p["device_id"] == disk_id), None)
        label = f"Disco ID {disk_id}"
        if pd and pd.get("slot") is not None:
            label = f"Slot {pd['slot']} (ID {disk_id})"

        key = f"disco.{disk_id}.estado"
        if pds is None:
            out.append(unknown(key, g, f"{label} · estado", pd_error or "MegaCLI sem resposta válida"))
        elif pd is None:
            out.append(check(key, g, f"{label} · estado", CRITICO, "Disco ausente",
                             "Não listado pela controladora"))
        else:
            status = _pd_status(pd)
            raised = [n for n in PD_COUNTERS
                      if pd.get(n) is not None and pd[n] > baselines.get(f"pd.{disk_id}.{n}", pd[n])]
            if raised and status == OK:
                status = ATENCAO
            value = " · ".join(str(x) for x in (pd.get("model"), pd.get("size"), pd.get("state")) if x)
            if pd.get("temperature") is not None:
                value += f" · {pd['temperature']} °C"
            detail = (f"Erros de mídia {pd.get('media_errors')} · outros erros {pd.get('other_errors')} · "
                      f"falhas preditivas {pd.get('predictive_failures')}")
            if pd.get("smart_alert"):
                detail += " · controladora sinalizou alerta S.M.A.R.T."
            if raised:
                detail += " · aumento desde a linha de base: " + ", ".join(PD_COUNTERS[n] for n in raised)
            out.append(check(key, g, f"{label} · estado", status, value, detail))

        key = f"disco.{disk_id}.smart"
        data = smart.get(disk_id)
        if not data:
            out.append(unknown(key, g, f"{label} · SMART", "smartctl sem resposta válida"))
            continue
        status = OK if data["health"].upper() == "OK" else CRITICO
        parts, raised = [], False
        for name, text in SMART_COUNTERS.items():
            if name not in data:
                continue
            base = baselines.get(f"smart.{disk_id}.{name}", data[name])
            mark = ""
            if data[name] > base:
                raised = True
                mark = " ▲"
            parts.append(f"{text}: {data[name]} (base {base}){mark}")
        if raised and status == OK:
            status = ATENCAO
        value = f"Saúde {data['health']}"
        if data.get("temperature") is not None:
            value += f" · {data['temperature']} °C"
        if data.get("power_on_hours") is not None:
            value += f" · {data['power_on_hours']} h ligado"
        out.append(check(key, g, f"{label} · SMART", status, value, " · ".join(parts)))
    return out


# ---------------------------------------------------------------- BBU

def bbu_checks(bbu, error=""):
    g = "bbu"
    if not bbu:
        return [unknown("bbu.estado", g, "Bateria BBU", error or "MegaCLI sem resposta válida")]

    def yes(key):
        return bbu.get(key, "").strip().lower() == "yes"

    problems, warnings = [], []
    for key, text in (("battery pack missing", "bateria ausente"),
                      ("battery replacement required", "substituição necessária"),
                      ("pack is about to fail & should be replaced", "bateria prestes a falhar")):
        if yes(key):
            problems.append(text)
    if bbu.get("issohgood", "").lower() == "no":
        problems.append("estado de saúde (SOH) ruim")
    if yes("remaining capacity low"):
        warnings.append("capacidade restante baixa")
    for key, text in (("voltage status", "tensão"), ("temperature status", "temperatura")):
        if key in bbu and bbu[key].strip().upper() != "OK":
            warnings.append(f"{text}: {bbu[key]}")

    state = bbu.get("battery state", "")
    s = state.lower()
    if "fail" in s:
        problems.append(f"estado {state}")
    elif state and s not in ("optimal", "operational") and "learn" not in s:
        warnings.append(f"estado {state}")

    status = CRITICO if problems else ATENCAO if warnings else OK
    value = state or "Estado não informado"
    if bbu.get("relative state of charge"):
        value += f" · carga {bbu['relative state of charge']}"
    if bbu.get("temperature"):
        value += f" · {bbu['temperature']}"
    details = problems + warnings
    if yes("learn cycle active"):
        details.append("ciclo de aprendizado em andamento")
    if not state and not details:
        status = DESCONHECIDO
    return [check("bbu.estado", g, "Bateria BBU", status, value, "; ".join(details))]


# ---------------------------------------------------------------- IPMI

_CRIT_STATUS = {"cr", "lcr", "ucr", "nr", "lnr", "unr"}
_WARN_STATUS = {"nc", "lnc", "unc"}
_CRIT_WORDS = ("failure", "fault", "lost", "fail")
_WARN_WORDS = ("predictive", "degraded", "non-redundant", "insufficient")


def ipmi_sensor_status(sensor):
    st = sensor["status"]
    reading = sensor["reading"].lower()
    if st == "ns" or reading in ("", "no reading") or reading.startswith("disabled"):
        return IGNORADO
    if st in _CRIT_STATUS:
        return CRITICO
    if st in _WARN_STATUS:
        return ATENCAO
    if st != "ok":
        return DESCONHECIDO
    if any(w in reading for w in _CRIT_WORDS):
        return CRITICO
    if any(w in reading for w in _WARN_WORDS):
        return ATENCAO
    return OK


def ipmi_group(sensor):
    name = sensor["name"].lower()
    reading = sensor["reading"].lower()
    if "redundancy" in name:
        return "ventilacao" if "fan" in name else "energia"
    if "rpm" in reading or name.startswith("fan"):
        return "ventilacao"
    if "degrees" in reading or "temp" in name:
        return "temperatura"
    if (sensor["entity"].startswith("10.") or any(u in reading for u in ("watts", "amps", "volts"))
            or name.startswith(("current", "voltage", "system level", "pwr consumption"))):
        return "energia"
    return "outros"


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def ipmi_checks(sensors, cfg, error=""):
    groups = ("energia", "ventilacao", "temperatura")
    if sensors is None or not sensors:
        reason = error or "ipmitool sem resposta válida"
        return [unknown(f"{g}.coleta", g, "Sensores IPMI", reason) for g in groups]

    out, names = [], {}
    for s in sensors:
        names.setdefault((ipmi_group(s), s["name"]), 0)
        names[(ipmi_group(s), s["name"])] += 1

    found = set()
    for s in sensors:
        g = ipmi_group(s)
        status = ipmi_sensor_status(s)
        if g == "outros" and status in (OK, IGNORADO):
            continue  # demais sensores só aparecem quando indicam problema
        label = s["name"]
        if g == "energia" and s["entity"].startswith("10."):
            label = f"Fonte {s['entity'].split('.', 1)[1]} · {s['name']}"
        elif names[(g, s["name"])] > 1:
            label = f"{s['name']} ({s['entity']})"
        detail = ""
        if status == OK and g == "ventilacao" and "rpm" in s["reading"].lower():
            match = re.search(r"\d+", s["reading"])
            if match and int(match.group()) < cfg.fan_min_rpm:
                status, detail = ATENCAO, f"Abaixo de {cfg.fan_min_rpm} RPM"
        if status in (OK, ATENCAO) and g == "temperatura" and re.search(r"ambient|inlet", s["name"], re.I):
            match = re.search(r"-?\d+", s["reading"])
            if match:
                temp = int(match.group())
                limit = by_threshold(temp, cfg.ambient_warn, cfg.ambient_crit)
                if limit != OK:
                    status = worst([status, limit])
                    detail = f"Limites do painel: atenção {cfg.ambient_warn} °C, crítico {cfg.ambient_crit} °C"
        if status == IGNORADO:
            detail = "Sensor desativado ou sem leitura (não gera alerta)"
        if status != IGNORADO:
            found.add(g)
        out.append(check(f"ipmi.{_slug(s['id'])}.{_slug(s['name'])}", g, label, status,
                         s["reading"], detail))
    for g in groups:
        if g not in found:
            out.append(check(f"{g}.ausente", g, "Sensores IPMI", DESCONHECIDO, "Sem sensores ativos",
                             "Nenhum sensor ativo encontrado neste grupo"))
    return out


_CHASSIS_FAULTS = {
    "Power Overload": "sobrecarga de energia",
    "Main Power Fault": "falha na alimentação principal",
    "Power Control Fault": "falha no controle de energia",
    "Drive Fault": "falha de disco",
    "Cooling/Fan Fault": "falha de ventilação",
}


def chassis_checks(chassis, sel, chassis_error="", sel_error=""):
    g = "chassis"
    out = []
    if not chassis or not any(k in chassis for k in _CHASSIS_FAULTS):
        out.append(unknown("chassis.falhas", g, "Indicadores do chassis",
                           chassis_error or "ipmitool sem resposta válida"))
    else:
        faults = [text for k, text in _CHASSIS_FAULTS.items() if chassis.get(k, "").lower() == "true"]
        value = "Falhas: " + ", ".join(faults) if faults else "Nenhuma falha indicada"
        detail = f"Energia do sistema: {chassis.get('System Power', '?')}"
        out.append(check("chassis.falhas", g, "Indicadores do chassis", CRITICO if faults else OK, value, detail))
    if not sel or sel.get("entries") is None:
        out.append(unknown("sel.uso", g, "Registro SEL", sel_error or "ipmitool sem resposta válida"))
    else:
        pct = sel.get("percent_used")
        status = by_threshold(pct, 80, 95) if pct is not None else OK
        value = f"{sel['entries']} registro(s)" + (f" · {pct}% ocupado" if pct is not None else "")
        out.append(check("sel.uso", g, "Registro SEL", status, value,
                         "Novos registros geram evento; o SEL cheio deixa de registrar falhas", pct))
    return out
