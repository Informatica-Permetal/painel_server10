"""Monta os dados das telas a partir da última coleta gravada (sem acessar hardware)."""
import re
from collections import Counter
from datetime import timedelta

from . import db
from .status import (ATENCAO, CRITICO, DESCONHECIDO, GROUPS, IGNORADO, OK, RANK, worst)

GROUP_ICONS = {
    "sistema": "cpu", "armazenamento": "database", "raid": "layers", "discos": "drive",
    "bbu": "battery", "energia": "power", "ventilacao": "fan", "temperatura": "temp",
    "chassis": "shield", "outros": "sliders", "coleta": "activity",
}
METER_ICONS = {"sistema.cpu": "cpu", "sistema.ram": "memory", "sistema.swap": "memory"}
HEADLINES = {
    OK: "Operando normalmente",
    ATENCAO: "Requer atenção",
    CRITICO: "Falha crítica",
    DESCONHECIDO: "Estado desconhecido",
}
COLLECT_INTERVAL = timedelta(minutes=5)


def _plural(n, one, many):
    return f"{n} {one if n == 1 else many}"


def summarize(checks):
    counted = [c for c in checks if c.get("alert", True) and c["status"] != IGNORADO]
    counts = Counter(c["status"] for c in counted)
    parts = []
    if counts[CRITICO]:
        parts.append(_plural(counts[CRITICO], "item crítico", "itens críticos"))
    if counts[ATENCAO]:
        parts.append(_plural(counts[ATENCAO], "item em atenção", "itens em atenção"))
    if counts[DESCONHECIDO]:
        parts.append(_plural(counts[DESCONHECIDO], "item sem leitura", "itens sem leitura"))
    if not parts:
        sentence = f"Todos os {len(counted)} itens monitorados estão normais."
    else:
        joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " e " + parts[-1]
        sentence = f"{joined[0].upper()}{joined[1:]}; {_plural(counts[OK], 'item normal', 'itens normais')}."
    problems = sorted((c for c in counted if c["status"] != OK), key=lambda c: -RANK[c["status"]])
    return {"total": len(counted), "counts": counts, "sentence": sentence, "problems": problems}


def hardware(checks):
    """Resumo para o desenho do painel frontal: discos, RAID, BBU, fontes, ventoinhas, ambiente."""
    by_key = {c["key"]: c for c in checks}
    hw = {"disks": [], "psus": [], "fans": [], "raid": None, "bbu": None, "ambient": None}

    for key in sorted(k for k in by_key if re.fullmatch(r"disco\.\d+\.estado", k)):
        disk_id = key.split(".")[1]
        parts = [by_key[key]] + ([by_key[f"disco.{disk_id}.smart"]] if f"disco.{disk_id}.smart" in by_key else [])
        name = by_key[key]["label"].split(" · ")[0]
        hw["disks"].append({"name": re.sub(r"\s*\(ID \d+\)", "", name), "title": name,
                            "status": worst(c["status"] for c in parts)})

    raid = [c for c in checks if c["group"] == "raid"]
    if raid:
        hw["raid"] = {"status": worst(c["status"] for c in raid),
                      "text": raid[0]["value"].split(" · ")[-1] if raid[0]["value"] else ""}
    bbu = by_key.get("bbu.estado")
    if bbu:
        hw["bbu"] = {"status": bbu["status"], "text": bbu["value"].split(" · ")[0]}

    for c in checks:
        if c["group"] == "energia":
            match = re.fullmatch(r"Fonte (\d+) · Status", c["label"])
            if match:
                hw["psus"].append({"name": f"PSU {match.group(1)}", "status": c["status"]})
        elif c["group"] == "ventilacao" and c["key"].startswith("ipmi.") and "redundancy" not in c["label"].lower():
            match = re.search(r"(\d+)", c["label"])
            hw["fans"].append({"name": match.group(1) if match else str(len(hw["fans"]) + 1),
                               "title": c["label"], "status": c["status"], "text": c["value"]})
        elif c["group"] == "temperatura" and re.search(r"ambient|inlet", c["label"], re.I):
            hw["ambient"] = {"status": c["status"], "text": c["value"]}

    if not hw["psus"]:
        energia = [c for c in checks if c["group"] == "energia"]
        if energia:
            hw["psus"].append({"name": "Fontes", "status": worst(c["status"] for c in energia)})
    return hw


def meters(checks):
    out = []
    for c in checks:
        if c["group"] not in ("sistema", "armazenamento") or not c.get("alert", True):
            continue
        text = re.sub(r"\s*\(\d+%\)$", "", c["value"] or "")
        out.append({"key": c["key"], "label": "Volume " + c["label"] if c["group"] == "armazenamento" else c["label"],
                    "icon": METER_ICONS.get(c["key"], "database"), "status": c["status"], "pct": c.get("pct"),
                    "text": text, "detail": c.get("detail", "")})
    return out


def availability(conn, now):
    """Percentual de coletas registradas nas últimas 24 h em relação ao esperado."""
    since = now - timedelta(hours=24)
    row = conn.execute("SELECT COUNT(*) AS n, MIN(ts) AS first FROM snapshots WHERE ts >= ?",
                       (since.isoformat(timespec="seconds"),)).fetchone()
    if not row["n"]:
        return None
    first = max(since, db.parse_ts(row["first"]))
    expected = int((now - first) / COLLECT_INTERVAL) + 1
    return {"count": row["n"], "pct": min(100, round(100 * row["n"] / max(expected, 1)))}


def age_text(minutes):
    if minutes < 1:
        return "agora"
    if minutes < 60:
        return f"há {minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"há {hours} h {rest} min" if rest else f"há {hours} h"
    return f"há {hours // 24} dias"


def dashboard(conn, cfg, now):
    snap = db.latest_snapshot(conn)
    view = {"snap": None, "events": db.recent_events(conn, 8)}
    if not snap:
        return view
    age_min = max(0, int((now - db.parse_ts(snap["ts"])).total_seconds() // 60))
    stale = age_min > cfg.stale_minutes
    checks = snap["checks"]
    overall = DESCONHECIDO if stale else snap["overall"]

    groups = []
    for gid, title in GROUPS:
        if gid in ("sistema", "armazenamento"):
            continue
        items = [c for c in checks if c["group"] == gid]
        if not items:
            continue
        active = [c for c in items if c["status"] != IGNORADO]
        groups.append({"id": gid, "title": title, "icon": GROUP_ICONS[gid],
                       "status": worst(c["status"] for c in items),
                       "checks": active, "ignored": [c for c in items if c["status"] == IGNORADO]})

    uptime = next((c["value"] for c in checks if c["key"] == "sistema.uptime" and c["status"] == OK), None)
    view.update(snap=snap, stale=stale, age_min=age_min, age_text=age_text(age_min), overall=overall,
                headline=HEADLINES[overall], summary=summarize(checks), hw=hardware(checks),
                meters=meters(checks), groups=groups, uptime=uptime, availability=availability(conn, now),
                group_titles=dict(GROUPS))
    return view
