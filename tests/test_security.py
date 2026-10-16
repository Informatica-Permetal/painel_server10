"""Verificações de segurança da interface: acesso, segredos, redirecionamentos e cabeçalhos."""
import re

import pytest
from werkzeug.security import generate_password_hash

from painel import db, web

from .conftest import T0, run_once

SENHA = "senha-de-teste-456"
SEGREDOS = {
    "secret_key": "chave-secreta-de-teste-0123456789",
    "smtp_password": "senha-smtp-de-teste-nao-exibir",
}


@pytest.fixture
def app(cfg, conn, runner):
    cfg.secret_key = SEGREDOS["secret_key"]
    cfg.smtp_password = SEGREDOS["smtp_password"]
    cfg.smtp_user = "usuario-smtp"
    cfg.admin_password_hash = generate_password_hash(SENHA)
    cfg.stale_minutes = 10 ** 7
    run_once(conn, cfg, runner, T0)
    db.add_recipient(conn, "ti@exemplo.invalid")
    conn.commit()
    return web.create_app(cfg)


def token(client, path="/login"):
    html = client.get(path, follow_redirects=True).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def login(client):
    return client.post("/login", data={"csrf_token": token(client), "usuario": "admin", "senha": SENHA})


def test_todas_as_rotas_exigem_login(app, cfg):
    client = app.test_client()
    csrf = token(client)  # sessão anônima com CSRF válido: só o login deve barrar
    public = {"login", "healthz", "static", "set_theme", "logout"}  # logout apenas limpa a sessão
    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint in public:
            continue
        url = re.sub(r"<int:\w+>", "1", rule.rule)
        if "GET" in rule.methods:
            resp = client.get(url)
            assert resp.status_code in (302, 401), url
            checked += 1
        if "POST" in rule.methods:
            resp = client.post(url, data={"csrf_token": csrf, "email": "x@exemplo.invalid"})
            assert resp.status_code in (302, 401), url
            assert "/login" in resp.headers.get("Location", "/login"), url
            checked += 1
    assert checked >= 10
    conn = db.connect(cfg.db_path)
    assert [r["email"] for r in db.list_recipients(conn)] == ["ti@exemplo.invalid"]  # nada foi alterado
    conn.close()


def test_segredos_nunca_aparecem_nas_respostas(app, cfg):
    client = app.test_client()
    login(client)
    bodies = [client.get(p).get_data(as_text=True)
              for p in ("/", "/parcial/status", "/api/status", "/eventos", "/admin", "/login", "/healthz")]
    bodies += [client.get(p).get_data(as_text=True) for p in ("/static/app.js", "/static/style.css")]
    joined = "\n".join(bodies)
    for value in list(SEGREDOS.values()) + [cfg.admin_password_hash]:
        assert value not in joined
    assert "usuario-smtp" in client.get("/admin").get_data(as_text=True)  # usuário pode aparecer, senha não


@pytest.mark.parametrize("target", ["//evil.example/x", "/\\evil.example", "https://evil.example/", "javascript:alert(1)"])
def test_redirecionamentos_externos_bloqueados(app, target):
    client = app.test_client()
    resp = client.post("/login", data={"csrf_token": token(client), "usuario": "admin", "senha": SENHA,
                                       "next": target})
    assert resp.headers["Location"] == "/"
    resp = client.post("/tema", data={"csrf_token": token(client), "tema": "claro", "voltar": target})
    assert resp.headers["Location"] == "/"


def test_tema_valida_valor_e_exige_csrf(app):
    client = app.test_client()
    assert client.post("/tema", data={"tema": "claro"}).status_code == 400
    resp = client.post("/tema", data={"csrf_token": token(client), "tema": "<script>"})
    assert "tema=" not in resp.headers.get("Set-Cookie", "")
    client.set_cookie("tema", "invalido")
    assert 'data-theme="dark"' in client.get("/login").get_data(as_text=True)
    client.set_cookie("tema", "sistema")
    assert 'data-theme="system"' in client.get("/login").get_data(as_text=True)


def test_usuario_com_acento_nao_quebra_login(app):
    client = app.test_client()
    resp = client.post("/login", data={"csrf_token": token(client), "usuario": "admín", "senha": "x"})
    assert resp.status_code == 401


def test_escape_de_entrada(app):
    client = app.test_client()
    login(client)
    html = client.get("/login?next=%22%3E%3Cscript%3Ealert(1)%3C/script%3E").get_data(as_text=True)
    assert "<script>alert(1)" not in html


def test_cabecalhos_de_seguranca_e_cache(app):
    client = app.test_client()
    page = client.get("/login")
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "DENY"
    assert page.headers["Cache-Control"] == "no-store"
    static = client.get("/static/style.css")
    assert static.headers["Cache-Control"].startswith("public")
    assert "unsafe-inline" not in page.headers["Content-Security-Policy"]


def test_painel_nao_executa_comandos(app, monkeypatch):
    """A interface só lê o banco: qualquer tentativa de executar processo falha o teste."""
    import subprocess

    def proibido(*_a, **_k):
        raise AssertionError("a interface web não pode executar comandos")

    monkeypatch.setattr(subprocess, "run", proibido)
    monkeypatch.setattr(subprocess, "Popen", proibido)
    client = app.test_client()
    login(client)
    for path in ("/", "/parcial/status", "/api/status", "/eventos", "/admin"):
        assert client.get(path).status_code == 200
