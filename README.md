# Painel Server10

Painel web leve para monitorar um servidor Dell PowerEdge R710 (PERC H700, RAID 1, discos SAS)
pela rede interna. Mostra CPU, RAM, swap, volumes, RAID, discos/SMART, BBU, fontes, ventoinhas,
temperaturas, chassis/SEL e tempo ligado, com estados **OK / Atenção / Crítico / Desconhecido**,
histórico de ocorrências e alertas por e-mail sem repetição.

## Arquitetura

| Parte | Executa como | Função |
| --- | --- | --- |
| `painel.collector` (timer de 5 min) | root | Roda comandos **fixos e somente de leitura** (MegaCLI, smartctl, ipmitool, /proc), avalia regras, grava no SQLite e envia e-mails |
| `painel.collector --watchdog` (timer de 10 min) | `painel` | Alerta se a coleta parar |
| `painel.web` (gunicorn com HTTPS na porta 8443) | `painel` | Só lê o banco e grava ajustes; não executa comandos nem tem acesso a dispositivos |

Dependências: Python 3, Flask e gunicorn. Banco SQLite em `/var/lib/painel-server10/painel.db`.

Interface: HTML/Jinja, CSS e JavaScript puros (~33 KB no total, com cache de 7 dias), ícones SVG locais e
fontes do sistema, sem CDN. Tema escuro por padrão, com opções claro e seguir o sistema (preferência
guardada em cookie não sensível). O painel recarrega só o trecho de estado a cada 60 s, pausando com a aba
oculta, e lê apenas a última coleta gravada: abrir o navegador nunca dispara consultas ao hardware. Os
estados usam forma de ícone e texto, além da cor.

Regras principais:

- Falha ou saída inesperada de um comando vira **Desconhecido**, nunca OK. Coleta mais antiga que
  `STALE_MINUTES` deixa o painel inteiro como Desconhecido.
- Contadores cumulativos SMART/PERC não são zerados: a linha de base vem de `SMART_BASELINE` (ou da
  primeira coleta) e só **aumentos** geram evento e alerta. Na administração é possível aceitar os
  valores atuais como nova base depois de analisar o aumento. Troca de disco (novo número de série)
  reinicia a base.
- Sensores IPMI `ns`/`Disabled` aparecem como "Desativado" e nunca geram alerta.
- Falhas atuais vêm do estado corrente dos sensores (sdr, chassis, MegaCLI, SMART), não do SEL. Novos
  registros no SEL geram um evento cada; a contagem existente na primeira coleta é só linha de base.
- E-mail consolidado por coleta, apenas em mudança de estado; intervalo mínimo por item
  (anti-oscilação), lembrete opcional para problemas persistentes e aviso de recuperação.

Nenhum comando grava na controladora, nos discos, no SEL ou no iDRAC; não há testes SMART automáticos.

## Desenvolvimento local

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # Linux: .venv/bin/python
.venv/Scripts/python -m pytest -q
```

Os testes usam saídas simuladas em `tests/fixtures/` e não acessam hardware.

## Instalação no servidor (Ubuntu 24.04)

Pré-requisitos: `python3-venv`, `openssl`, acesso à internet para o `pip` (ou um mirror interno).

```bash
git clone https://github.com/Informatica-Permetal/painel_server10.git
cd painel_server10
sudo ./deploy/install.sh
```

O script cria o usuário `painel`, copia o código para `/opt/painel-server10`, cria o virtualenv,
o diretório de dados, `/etc/painel-server10/painel.env` (0600, com `SECRET_KEY` gerada) e um
certificado autoassinado em `/etc/painel-server10/tls/`. Depois:

1. Gere o hash da senha administrativa e preencha `/etc/painel-server10/painel.env`
   (`ADMIN_PASSWORD_HASH`, `MEGACLI_PATH`, `MEGACLI_LIBDIR`, `SMART_BASELINE`, `PANEL_URL` e, se houver, `SMTP_*`):
   `cd /opt/painel-server10 && sudo -u painel venv/bin/python -m painel.manage hash-password`
2. Teste uma coleta: `sudo systemctl start painel-server10-collector.service` e
   `journalctl -u painel-server10-collector.service -n 20`.
3. Habilite: `sudo systemctl enable --now painel-server10-web.service painel-server10-collector.timer painel-server10-watchdog.timer`.
4. Acesse `https://<ip-do-servidor>:8443` de outro PC da rede. O certificado é autoassinado: o navegador
   pedirá confirmação, ou importe `painel.crt` como confiável nos PCs.

Para atualizar, rode `git pull` e `sudo ./deploy/install.sh` novamente; a configuração existente é
preservada. Depois `sudo systemctl restart painel-server10-web.service`.

A porta 8443 deve ficar acessível somente pela LAN; não publique no pfSense para a internet.

## Manutenção

- Logs: `journalctl -u painel-server10-web -u painel-server10-collector -u painel-server10-watchdog`
- Próximas execuções: `systemctl list-timers 'painel-server10-*'`
- Configuração privada: `/etc/painel-server10/painel.env` (nunca versionar)
- Dados: `/var/lib/painel-server10/painel.db` (coletas por 7 dias, eventos por 1 ano)

## Segurança

Login com hash scrypt, cookie de sessão `Secure`/`HttpOnly`, CSRF em todas as ações, limite de
tentativas de login, cabeçalhos CSP/anti-frame, serviço web sem root e sem rota que execute comandos.
O repositório é público: segredos, certificados, bancos e arquivos `.env` estão no `.gitignore`.
