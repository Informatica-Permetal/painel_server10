"""Estados, severidades e grupos exibidos no painel."""

OK = "OK"
ATENCAO = "ATENCAO"
CRITICO = "CRITICO"
DESCONHECIDO = "DESCONHECIDO"
IGNORADO = "IGNORADO"  # sensor desativado (ns/Disabled): exibido, mas nunca alerta

LABELS = {
    OK: "OK",
    ATENCAO: "Atenção",
    CRITICO: "Crítico",
    DESCONHECIDO: "Desconhecido",
    IGNORADO: "Desativado",
}

# Ordem usada para calcular o pior estado de um conjunto.
RANK = {IGNORADO: -1, OK: 0, ATENCAO: 1, DESCONHECIDO: 2, CRITICO: 3}

# Severidade para e-mail: "somente críticos" inclui DESCONHECIDO, porque uma
# leitura ausente pode esconder uma falha real.
SEVERITY = {OK: 0, IGNORADO: 0, ATENCAO: 1, DESCONHECIDO: 2, CRITICO: 3}
MIN_SEVERITY_OPTIONS = {"ATENCAO": 1, "CRITICO": 2}

GROUPS = [
    ("sistema", "Sistema"),
    ("armazenamento", "Armazenamento"),
    ("raid", "RAID (PERC H700)"),
    ("discos", "Discos SAS e SMART"),
    ("bbu", "Bateria da controladora (BBU)"),
    ("energia", "Fontes e energia"),
    ("ventilacao", "Ventilação"),
    ("temperatura", "Temperaturas"),
    ("chassis", "Chassis e SEL"),
    ("outros", "Outros sensores IPMI"),
    ("coleta", "Coleta"),
]
GROUP_TITLES = dict(GROUPS)


def worst(statuses):
    """Pior estado de uma lista, ignorando sensores desativados."""
    result = None
    for status in statuses:
        if status == IGNORADO:
            continue
        if result is None or RANK[status] > RANK[result]:
            result = status
    return result or DESCONHECIDO
