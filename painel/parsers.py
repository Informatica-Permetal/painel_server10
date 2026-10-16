"""Parsers das saídas de MegaCLI, smartctl, ipmitool e /proc.

Recebem texto e devolvem estruturas simples; nunca executam comandos.
Quando o texto não tem o formato esperado retornam None (ou lista vazia),
para que a coleta seja tratada como DESCONHECIDA e nunca como sucesso.
"""
import re


def _int(text):
    match = re.search(r"-?\d+", text or "")
    return int(match.group()) if match else None


def _split_kv(line, sep=":"):
    if sep not in line:
        return None, None
    key, value = line.split(sep, 1)
    return key.strip(), value.strip()


# ---------------------------------------------------------------- MegaCLI

def _raid_level(text):
    match = re.search(r"Primary-(\d+)", text)
    return f"RAID {match.group(1)}" if match else text


def parse_megacli_ldinfo(text):
    vds, cur = [], None
    for line in text.splitlines():
        match = re.match(r"\s*Virtual Drive\s*:\s*(\d+)", line)
        if match:
            cur = {"id": int(match.group(1))}
            vds.append(cur)
            continue
        if cur is None:
            continue
        key, value = _split_kv(line)
        if key == "Name":
            cur.setdefault("name", value)
        elif key == "RAID Level":
            cur.setdefault("raid_level", _raid_level(value))
        elif key == "Size":
            cur.setdefault("size", value)
        elif key == "State":
            cur.setdefault("state", value)
        elif key == "Number Of Drives":
            cur.setdefault("drives", _int(value))
    return [vd for vd in vds if "state" in vd]


_PD_INT_FIELDS = {
    "Slot Number": "slot",
    "Device Id": "device_id",
    "Media Error Count": "media_errors",
    "Other Error Count": "other_errors",
    "Predictive Failure Count": "predictive_failures",
    "Drive Temperature": "temperature",
}


def parse_megacli_pdlist(text):
    disks, cur = [], None
    for line in text.splitlines():
        key, value = _split_kv(line)
        if key is None:
            continue
        if key == "Enclosure Device ID":
            cur = {"enclosure": value}
            disks.append(cur)
            continue
        if cur is None:
            continue
        if key in _PD_INT_FIELDS:
            cur[_PD_INT_FIELDS[key]] = _int(value)
        elif key == "Firmware state":
            cur["state"] = value
        elif key == "Inquiry Data":
            cur["inquiry"] = " ".join(value.split())
            parts = cur["inquiry"].split()
            cur["model"] = parts[1] if len(parts) > 1 else cur["inquiry"]
        elif key == "Drive has flagged a S.M.A.R.T alert":
            cur["smart_alert"] = value.lower().startswith("yes")
        elif key == "PD Type":
            cur["type"] = value
        elif key == "Raw Size":
            cur["size"] = value.split("[")[0].strip()
    return [d for d in disks if d.get("device_id") is not None]


def parse_megacli_bbu(text):
    """Retorna dict com chaves em minúsculas; chaves repetidas ganham sufixo ' status'."""
    info = {}
    for line in text.splitlines():
        key, value = _split_kv(line)
        if not key:
            continue
        key = key.lower()
        if key in info:
            info[f"{key} status"] = value
        else:
            info[key] = value
    markers = ("battery state", "issohgood", "battery pack missing",
               "battery replacement required", "batterytype")
    if not any(m in info for m in markers):
        return None
    return info


# ---------------------------------------------------------------- smartctl

def parse_smartctl(text):
    data = {}
    match = re.search(r"SMART Health Status:\s*(.+)", text)
    if match:
        data["health"] = match.group(1).strip()
    else:
        match = re.search(r"SMART overall-health self-assessment test result:\s*(\S+)", text)
        if match:
            data["health"] = "OK" if match.group(1).upper() == "PASSED" else match.group(1)
    if "health" not in data:
        return None

    for key, pattern in (("vendor", r"^Vendor:\s*(.+)$"),
                         ("product", r"^Product:\s*(.+)$"),
                         ("serial", r"^Serial number:\s*(.+)$")):
        match = re.search(pattern, text, re.M)
        if match:
            data[key] = match.group(1).strip()

    for key, pattern in (("temperature", r"Current Drive Temperature:\s*(\d+)"),
                         ("grown_defects", r"Elements in grown defect list:\s*(\d+)"),
                         ("non_medium", r"Non-medium error count:\s*(\d+)")):
        match = re.search(pattern, text)
        if match:
            data[key] = int(match.group(1))

    for op in ("read", "write", "verify"):
        match = re.search(rf"^{op}:\s+(.+)$", text, re.M)
        if match:
            last = match.group(1).split()[-1]
            if last.isdigit():
                data[f"{op}_uncorrected"] = int(last)

    match = re.search(r"Accumulated power on time, hours:minutes\s+(\d+):", text)
    if match:
        data["power_on_hours"] = int(match.group(1))
    else:
        match = re.search(r"number of hours powered up\s*=\s*([\d.]+)", text)
        if match:
            data["power_on_hours"] = int(float(match.group(1)))
    return data


# ---------------------------------------------------------------- ipmitool

def parse_ipmi_sdr_elist(text):
    """Linhas no formato 'Nome | 30h | ok | 7.1 | leitura'."""
    sensors = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 5 or not parts[0]:
            continue
        name, sid, status, entity, reading = parts[:5]
        sensors.append({"name": name, "id": sid, "status": status.lower(),
                        "entity": entity, "reading": reading})
    return sensors


def parse_kv(text):
    info = {}
    for line in text.splitlines():
        key, value = _split_kv(line)
        if key:
            info[key] = value
    return info


def parse_ipmi_sel_info(text):
    info = parse_kv(text)
    if "Entries" not in info:
        return None
    return {"entries": _int(info["Entries"]), "percent_used": _int(info.get("Percent Used", ""))}


def parse_sel_lines(text):
    lines = []
    for line in text.splitlines():
        if "|" in line:
            lines.append(" | ".join(p.strip() for p in line.split("|")))
    return lines


# ---------------------------------------------------------------- /proc

def parse_meminfo(text):
    info = {}
    for line in text.splitlines():
        key, value = _split_kv(line)
        if key:
            number = _int(value)
            if number is not None:
                info[key] = number  # kB
    return info


def parse_loadavg(text):
    parts = text.split()
    return [float(p) for p in parts[:3]]


def parse_uptime(text):
    return float(text.split()[0])


def parse_cpu_stat(text):
    """Retorna (total, ocioso, quantidade_de_cpus)."""
    total = idle = None
    ncpu = 0
    for line in text.splitlines():
        if line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:]]
            total = sum(values[:8])
            idle = values[3] + (values[4] if len(values) > 4 else 0)
        elif re.match(r"cpu\d+ ", line):
            ncpu += 1
    if total is None:
        raise ValueError("linha 'cpu' ausente em /proc/stat")
    return total, idle, ncpu


def cpu_percent(first, second):
    total = second[0] - first[0]
    if total <= 0:
        return None
    return max(0.0, min(100.0, 100.0 * (1 - (second[1] - first[1]) / total)))


REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "zfs", "jfs"}


def parse_mounts(text):
    """Pontos de montagem de sistemas de arquivos reais, sem duplicar dispositivos."""
    seen, mounts = set(), []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[2] not in REAL_FS:
            continue
        device = parts[0]
        mount = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
        if device in seen:
            continue
        seen.add(device)
        mounts.append(mount)
    return mounts
