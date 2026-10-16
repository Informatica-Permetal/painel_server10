#!/usr/bin/env bash
# Instala ou atualiza o Painel Server10 no Ubuntu. Executar como root a partir do clone:
#
#   sudo ./deploy/install.sh            # instala/atualiza arquivos, sem iniciar serviços
#   sudo ./deploy/install.sh --enable   # também habilita e inicia web + timers
#
# O script NÃO altera RAID, discos, SEL, iDRAC, firewall, rede, SSH ou outros serviços.
# Não instala pacotes do sistema: apenas verifica e informa o que faltar.
set -euo pipefail

APP_DIR=/opt/painel-server10
DATA_DIR=/var/lib/painel-server10
CONF_DIR=/etc/painel-server10
TLS_DIR=$CONF_DIR/tls
PORT=8443
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENABLE=0
[[ "${1:-}" == "--enable" ]] && ENABLE=1

[[ $EUID -eq 0 ]] || { echo "Execute como root (sudo)." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 não encontrado." >&2; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null || {
  echo "Módulo venv indisponível. Instale com: apt install python3-venv" >&2; exit 1; }
command -v openssl >/dev/null || { echo "openssl não encontrado." >&2; exit 1; }

if ss -ltnH "sport = :$PORT" | grep -q . && ! systemctl is-active --quiet painel-server10-web; then
  echo "A porta $PORT já está em uso por outro serviço. Ajuste o --bind no unit antes de continuar." >&2
  exit 1
fi

echo "==> Usuário de serviço"
id painel &>/dev/null || useradd --system --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin painel

echo "==> Código em $APP_DIR"
install -d -o root -g root -m 0755 "$APP_DIR"
rm -rf "$APP_DIR/painel"
cp -r "$SRC_DIR/painel" "$APP_DIR/painel"
find "$APP_DIR/painel" -name '__pycache__' -prune -exec rm -rf {} +
install -o root -g root -m 0644 "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
chown -R root:root "$APP_DIR/painel"
chmod -R u=rwX,go=rX "$APP_DIR/painel"

echo "==> Ambiente virtual Python"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --disable-pip-version-check -r "$APP_DIR/requirements.txt"

echo "==> Dados e configuração"
install -d -o painel -g painel -m 2770 "$DATA_DIR"
install -d -o root -g painel -m 0750 "$CONF_DIR"
if [[ ! -f "$CONF_DIR/painel.env" ]]; then
  install -o root -g root -m 0600 "$SRC_DIR/.env.example" "$CONF_DIR/painel.env"
  SECRET=$("$APP_DIR/venv/bin/python" -c 'import secrets; print(secrets.token_hex(32))')
  sed -i "s/^SECRET_KEY=.*/SECRET_KEY=$SECRET/" "$CONF_DIR/painel.env"
  echo "    Criado $CONF_DIR/painel.env (0600). Edite ADMIN_PASSWORD_HASH, MEGACLI_*, SMART_BASELINE e SMTP_*."
else
  chown root:root "$CONF_DIR/painel.env"; chmod 0600 "$CONF_DIR/painel.env"
  echo "    $CONF_DIR/painel.env existente preservado."
fi

if [[ ! -f "$TLS_DIR/painel.crt" ]]; then
  echo "==> Certificado autoassinado para a rede interna"
  install -d -o root -g painel -m 0750 "$TLS_DIR"
  HOST=$(hostname)
  IP=${PAINEL_IP:-$(hostname -I | awk '{print $1}')}
  openssl req -x509 -newkey rsa:3072 -sha256 -days 1825 -nodes \
    -keyout "$TLS_DIR/painel.key" -out "$TLS_DIR/painel.crt" \
    -subj "/CN=$HOST" -addext "subjectAltName=DNS:$HOST,IP:$IP" 2>/dev/null
  chown root:painel "$TLS_DIR/painel.key" "$TLS_DIR/painel.crt"
  chmod 0640 "$TLS_DIR/painel.key"; chmod 0644 "$TLS_DIR/painel.crt"
  echo "    Certificado para $HOST / $IP em $TLS_DIR (navegadores pedirão confirmação de confiança)."
fi

echo "==> Banco de dados"
(cd "$APP_DIR" && runuser -u painel -- env DB_PATH="$DATA_DIR/painel.db" \
  "$APP_DIR/venv/bin/python" -m painel.manage init-db)

echo "==> Units do systemd"
install -o root -g root -m 0644 "$SRC_DIR"/deploy/systemd/*.service "$SRC_DIR"/deploy/systemd/*.timer \
  /etc/systemd/system/
systemctl daemon-reload

UNITS="painel-server10-web.service painel-server10-collector.timer painel-server10-watchdog.timer"
if [[ $ENABLE -eq 1 ]]; then
  systemctl enable --now $UNITS
  systemctl restart painel-server10-web.service
  systemctl start painel-server10-collector.service || true
  systemctl --no-pager --lines=0 status $UNITS || true
else
  cat <<EOF

Instalação concluída (serviços ainda não iniciados). Próximos passos:
  1. sudo nano $CONF_DIR/painel.env
     - ADMIN_PASSWORD_HASH: cd $APP_DIR && sudo -u painel venv/bin/python -m painel.manage hash-password
     - MEGACLI_PATH / MEGACLI_LIBDIR, SMART_BASELINE, PANEL_URL, SMTP_* (opcional)
  2. Teste uma coleta:  sudo systemctl start painel-server10-collector.service
                        journalctl -u painel-server10-collector.service -n 20
  3. Habilite tudo:     sudo systemctl enable --now $UNITS
  4. Acesse:            https://$(hostname -I | awk '{print $1}'):$PORT
EOF
fi
