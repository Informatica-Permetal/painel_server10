import re

import pytest
from werkzeug.security import generate_password_hash

from painel import db, web

from .conftest import T0, run_once

SENHA = "senha-de-teste-123"


@pytest.fixture
def app(cfg, conn, runner):
    cfg.admin_user = "admin"
    cfg.admin_password_hash = generate_password_hash(SENHA)
    cfg.stale_minutes = 10 ** 7  # fixture com data fixa não deve aparecer como "parada"
    run_once(conn, cfg, runner, T0)
    application = web.create_app(cfg)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client, password=SENHA):
    token = csrf(client)
    return client.post("/login", data={"csrf_token": token, "usuario": "admin", "senha": password})


def test_painel_exige_login_por_padrao(client):
    assert client.get("/").status_code == 302
    assert client.get("/api/status").status_code == 401
    assert client.get("/admin").status_code == 302
    assert client.get("/healthz").status_code == 200


def test_login_invalido_e_valido(client):
    assert login(client, "errada").status_code == 401
    resp = login(client)
    assert resp.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "server-teste" in page
    assert "RAID1-TESTE" in page
    assert "Desativado" in page  # sensores ns/Disabled
    assert client.get("/api/status").get_json()["overall"] == "OK"


def test_bloqueio_apos_tentativas(client):
    for _ in range(5):
        login(client, "errada")
    assert login(client).status_code == 429


def test_post_sem_csrf_rejeitado(client):
    login(client)
    assert client.post("/admin/destinatarios", data={"email": "x@exemplo.invalid"}).status_code == 400


def test_destinatarios_crud(client, cfg):
    login(client)
    token = csrf(client, "/admin")
    client.post("/admin/destinatarios", data={"csrf_token": token, "email": "TI@Exemplo.invalid"})
    client.post("/admin/destinatarios", data={"csrf_token": token, "email": "outro@exemplo.invalid"})
    resp = client.post("/admin/destinatarios", data={"csrf_token": token, "email": "invalido@"},
                       follow_redirects=True)
    assert "inválido" in resp.get_data(as_text=True)
    resp = client.post("/admin/destinatarios", data={"csrf_token": token, "email": "ti@exemplo.invalid"},
                       follow_redirects=True)
    assert "já está cadastrado" in resp.get_data(as_text=True)

    conn = db.connect(cfg.db_path)
    rows = {r["email"]: r for r in db.list_recipients(conn)}
    assert set(rows) == {"ti@exemplo.invalid", "outro@exemplo.invalid"}
    client.post(f"/admin/destinatarios/{rows['outro@exemplo.invalid']['id']}/alternar", data={"csrf_token": token})
    assert db.enabled_recipients(conn) == ["ti@exemplo.invalid"]
    client.post(f"/admin/destinatarios/{rows['ti@exemplo.invalid']['id']}/remover", data={"csrf_token": token})
    assert [r["email"] for r in db.list_recipients(conn)] == ["outro@exemplo.invalid"]
    conn.close()


def test_configuracoes_validacao(client, cfg):
    login(client)
    token = csrf(client, "/admin")
    client.post("/admin/configuracoes", data={"csrf_token": token, "min_severity": "CRITICO",
                                               "cooldown_minutes": "60", "reminder_hours": "0"})
    conn = db.connect(cfg.db_path)
    settings = db.get_settings(conn)
    assert settings["min_severity"] == "CRITICO" and settings["alerts_enabled"] == "0"
    client.post("/admin/configuracoes", data={"csrf_token": token, "min_severity": "X",
                                               "cooldown_minutes": "1", "reminder_hours": "0"})
    assert db.get_settings(conn)["min_severity"] == "CRITICO"
    conn.close()


def test_email_teste_sem_smtp_e_senha_nao_exibida(client, cfg, monkeypatch):
    cfg.smtp_host = ""
    cfg.smtp_password = "segredo-nao-pode-aparecer"
    login(client)
    token = csrf(client, "/admin")
    page = client.get("/admin").get_data(as_text=True)
    assert "Não configurado" in page and "segredo-nao-pode-aparecer" not in page
    resp = client.post("/admin/teste-email", data={"csrf_token": token}, follow_redirects=True)
    assert "E-mail não configurado" in resp.get_data(as_text=True)


def test_email_teste_envia(client, cfg, monkeypatch):
    sent = []
    monkeypatch.setattr(web.mailer, "send_mail", lambda c, r, s, b: sent.append((r, s)))
    login(client)
    token = csrf(client, "/admin")
    client.post("/admin/destinatarios", data={"csrf_token": token, "email": "ti@exemplo.invalid"})
    resp = client.post("/admin/teste-email", data={"csrf_token": token}, follow_redirects=True)
    assert "E-mail de teste enviado" in resp.get_data(as_text=True)
    assert sent == [(["ti@exemplo.invalid"], "[Painel Server10] E-mail de teste")]


def test_redirect_externo_bloqueado(client):
    token = csrf(client)
    resp = client.post("/login", data={"csrf_token": token, "usuario": "admin", "senha": SENHA,
                                       "next": "//evil.example/x"})
    assert resp.headers["Location"] == "/admin"


def test_painel_publico_opcional(cfg, conn, runner):
    cfg.public_dashboard = True
    run_once(conn, cfg, runner, T0)
    client = web.create_app(cfg).test_client()
    assert client.get("/").status_code == 200
    assert client.get("/admin").status_code == 302


def test_coleta_antiga_aparece_como_desconhecida(cfg, conn, runner):
    cfg.public_dashboard = True
    cfg.stale_minutes = 15
    run_once(conn, cfg, runner, T0)  # data fixa no passado
    data = web.create_app(cfg).test_client().get("/api/status").get_json()
    assert data["stale"] is True and data["overall"] == "DESCONHECIDO"
