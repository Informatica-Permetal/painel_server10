"""Interface web (Flask). Executa sem privilégios e nunca roda comandos do sistema.

Apenas lê a última coleta do SQLite e grava ajustes de alertas/destinatários.
"""
import hmac
import os
import re
import secrets
import socket
import sys
import time
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash

from . import __version__, alerts, db, mailer, view
from .config import load_config
from .status import DESCONHECIDO, LABELS, MIN_SEVERITY_OPTIONS

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")
MAX_RECIPIENTS = 50
LOGIN_WINDOW = 15 * 60
LOGIN_MAX_FAILURES = 5
THEMES = {"escuro": "dark", "claro": "light", "sistema": "system"}
DEFAULT_THEME = "escuro"
EVENT_FILTERS = ("CRITICO", "ATENCAO", "DESCONHECIDO", "OK")
STATUS_ICONS = {"OK": "ok", "ATENCAO": "warn", "CRITICO": "crit", "DESCONHECIDO": "unk", "IGNORADO": "off"}
PUBLIC_ENDPOINTS = {"login", "healthz", "static", "set_theme"}


def build_view(conn, cfg, now):
    """Compatibilidade: visão do painel principal."""
    return view.dashboard(conn, cfg, now)


def create_app(cfg=None):
    cfg = cfg or load_config()
    app = Flask(__name__)
    secret = cfg.secret_key
    if not secret:
        secret = secrets.token_hex(32)
        print("AVISO: SECRET_KEY não definida; usando chave temporária (sessões expiram ao reiniciar).",
              file=sys.stderr)
    app.config.update(
        SECRET_KEY=secret,
        SESSION_COOKIE_NAME="painel_sessao",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=cfg.cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=64 * 1024,
    )
    app.extensions["painel_cfg"] = cfg
    failures = {}  # ip -> [timestamps]; limite simples por processo

    # ------------------------------------------------------------ infraestrutura

    def get_db():
        if "db" not in g:
            g.db = db.connect(cfg.db_path)
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400, "Token CSRF inválido. Recarregue a página e tente novamente.")

    @app.after_request
    def security_headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'; object-src 'none'")
        resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        resp.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        if request.path.startswith("/static/"):
            # URLs versionadas (?v=): o navegador reaproveita CSS/JS entre atualizações
            resp.headers["Cache-Control"] = "public, max-age=604800"
        else:
            resp.headers["Cache-Control"] = "no-store"
        if cfg.cookie_secure:
            resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        return resp

    def is_admin():
        return bool(session.get("user"))

    def login_required(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not is_admin():
                if request.path.startswith(("/api/", "/parcial/")):
                    return jsonify({"erro": "autenticação necessária"}), 401
                return redirect(url_for("login", next=request.path))
            return fn(*args, **kwargs)
        return wrapper

    def dashboard_access(fn):
        return fn if cfg.public_dashboard else login_required(fn)

    def fmt_dt(value):
        try:
            return db.parse_ts(value).strftime("%d/%m/%Y %H:%M:%S")
        except (TypeError, ValueError):
            return value or "—"

    def fmt_time(value):
        try:
            return db.parse_ts(value).strftime("%d/%m %H:%M")
        except (TypeError, ValueError):
            return value or "—"

    def theme_name():
        value = request.cookies.get("tema", DEFAULT_THEME)
        return value if value in THEMES else DEFAULT_THEME

    static_versions = {}

    def static_url(filename):
        """URL com versão pelo conteúdo publicado: cache longo sem servir CSS/JS desatualizado."""
        if filename not in static_versions:
            try:
                mtime = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
            except OSError:
                mtime = 0
            static_versions[filename] = f"{__version__}.{mtime}"
        return url_for("static", filename=filename, v=static_versions[filename])

    app.jinja_env.globals.update(
        csrf_token=csrf_token, is_admin=is_admin, LABELS=LABELS, version=__version__,
        public_dashboard=cfg.public_dashboard, theme_name=theme_name, THEMES=THEMES,
        static_url=static_url, STATUS_ICONS=STATUS_ICONS)
    app.jinja_env.filters["dt"] = fmt_dt
    app.jinja_env.filters["hm"] = fmt_time
    app.jinja_env.filters["status_label"] = lambda s: LABELS.get(s, s or "—")
    app.jinja_env.filters["status_class"] = lambda s: (s or DESCONHECIDO).lower()

    def now():
        return datetime.now().astimezone()

    # ------------------------------------------------------------ painel

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.post("/tema")
    def set_theme():
        """Preferência visual (cookie não sensível). Usado quando o JavaScript está desativado."""
        choice = request.form.get("tema", "")
        resp = redirect(safe_local(request.form.get("voltar", ""), url_for("index")))
        if choice in THEMES:
            resp.set_cookie("tema", choice, max_age=365 * 86400, samesite="Lax", secure=cfg.cookie_secure)
        return resp

    @app.get("/")
    @dashboard_access
    def index():
        return render_template("index.html", **view.dashboard(get_db(), cfg, now()))

    @app.get("/parcial/status")
    @dashboard_access
    def partial_status():
        return render_template("_status.html", **view.dashboard(get_db(), cfg, now()))

    @app.get("/api/status")
    @dashboard_access
    def api_status():
        data = view.dashboard(get_db(), cfg, now())
        if not data["snap"]:
            return jsonify({"overall": DESCONHECIDO, "ts": None, "checks": []})
        return jsonify({"hostname": data["snap"]["hostname"], "ts": data["snap"]["ts"],
                        "overall": data["overall"], "stale": data["stale"],
                        "age_minutes": data["age_min"], "checks": data["snap"]["checks"]})

    @app.get("/eventos")
    @dashboard_access
    def events():
        status = request.args.get("estado", "")
        status = status if status in EVENT_FILTERS else ""
        conn = get_db()
        return render_template("events.html", events=db.recent_events(conn, 500, status or None),
                               current=status, counts=db.count_events_by_status(conn),
                               filters=EVENT_FILTERS)

    # ------------------------------------------------------------ login

    def safe_local(target, fallback):
        if not target:
            return fallback
        parsed = urlparse(target)
        if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//") \
                or "\\" in target:
            return fallback
        return target

    def prune_failures():
        limit = time.time() - LOGIN_WINDOW
        for ip in [ip for ip, times in failures.items() if not times or times[-1] < limit]:
            del failures[ip]

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            if is_admin():
                return redirect(url_for("index"))
            return render_template("login.html", next=request.args.get("next", ""),
                                   configured=bool(cfg.admin_password_hash))
        if len(failures) > 500:
            prune_failures()
        ip = request.remote_addr or "?"
        recent = [t for t in failures.get(ip, []) if time.time() - t < LOGIN_WINDOW]
        failures[ip] = recent
        if len(recent) >= LOGIN_MAX_FAILURES:
            flash("Muitas tentativas seguidas. Aguarde 15 minutos e tente novamente.", "erro")
            return render_template("login.html", next="", configured=True), 429
        user = request.form.get("usuario", "")
        password = request.form.get("senha", "")
        valid = bool(cfg.admin_password_hash) and hmac.compare_digest(
            user.encode("utf-8"), cfg.admin_user.encode("utf-8"))
        try:
            valid = check_password_hash(cfg.admin_password_hash, password) and valid
        except ValueError:
            valid = False
        if not valid:
            failures[ip].append(time.time())
            flash("Usuário ou senha incorretos. Verifique e tente novamente.", "erro")
            return render_template("login.html", next=request.form.get("next", ""),
                                   configured=bool(cfg.admin_password_hash)), 401
        failures.pop(ip, None)
        session.clear()
        session.permanent = True
        session["user"] = cfg.admin_user
        session["csrf"] = secrets.token_urlsafe(32)
        return redirect(safe_local(request.form.get("next", ""), url_for("index")))

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------ administração

    @app.get("/admin")
    @login_required
    def admin():
        conn = get_db()
        recipients = db.list_recipients(conn)
        return render_template(
            "admin.html",
            settings=db.get_settings(conn),
            recipients=recipients,
            active_count=sum(1 for r in recipients if r["enabled"]),
            counters=[c for c in db.list_counters(conn) if c["key"].startswith(("smart.", "pd."))],
            disk_ids=cfg.disk_ids,
            max_recipients=MAX_RECIPIENTS,
            smtp={"configured": mailer.smtp_configured(cfg), "host": cfg.smtp_host, "port": cfg.smtp_port,
                  "sender": cfg.smtp_from, "user": cfg.smtp_user,
                  "security": "SSL" if cfg.smtp_ssl else "STARTTLS" if cfg.smtp_starttls else "sem TLS"},
            severity_options=MIN_SEVERITY_OPTIONS,
        )

    @app.post("/admin/configuracoes")
    @login_required
    def save_settings():
        try:
            cooldown = int(request.form.get("cooldown_minutes", ""))
            reminder = int(request.form.get("reminder_hours", ""))
        except ValueError:
            flash("Informe números inteiros nos intervalos.", "erro")
            return redirect(url_for("admin"))
        severity = request.form.get("min_severity", "")
        if severity not in MIN_SEVERITY_OPTIONS or not 5 <= cooldown <= 1440 or not 0 <= reminder <= 168:
            flash("Valores fora do permitido: intervalo de 5 a 1440 min e lembrete de 0 a 168 h.", "erro")
            return redirect(url_for("admin"))
        conn = get_db()
        db.set_setting(conn, "alerts_enabled", "1" if request.form.get("alerts_enabled") else "0")
        db.set_setting(conn, "min_severity", severity)
        db.set_setting(conn, "cooldown_minutes", cooldown)
        db.set_setting(conn, "reminder_hours", reminder)
        conn.commit()
        flash("Regras de alerta salvas.", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/destinatarios")
    @login_required
    def add_recipient():
        email = request.form.get("email", "").strip().lower()
        conn = get_db()
        if len(email) > 254 or not EMAIL_RE.match(email):
            flash("Endereço de e-mail inválido. Use o formato nome@empresa.com.br.", "erro")
        elif len(db.list_recipients(conn)) >= MAX_RECIPIENTS:
            flash(f"Limite de {MAX_RECIPIENTS} destinatários atingido. Remova um antes de adicionar.", "erro")
        else:
            try:
                db.add_recipient(conn, email)
                conn.commit()
                flash(f"{email} adicionado e ativo.", "ok")
            except db.sqlite3.IntegrityError:
                flash("Este endereço já está cadastrado.", "erro")
        return redirect(url_for("admin"))

    @app.post("/admin/destinatarios/<int:rid>/alternar")
    @login_required
    def toggle_recipient(rid):
        conn = get_db()
        db.toggle_recipient(conn, rid)
        conn.commit()
        return redirect(url_for("admin"))

    @app.post("/admin/destinatarios/<int:rid>/remover")
    @login_required
    def delete_recipient(rid):
        conn = get_db()
        db.delete_recipient(conn, rid)
        conn.commit()
        flash("Destinatário removido.", "ok")
        return redirect(url_for("admin"))

    @app.post("/admin/teste-email")
    @login_required
    def test_email():
        conn = get_db()
        recipients = db.enabled_recipients(conn)
        if not mailer.smtp_configured(cfg):
            flash("E-mail não configurado: defina SMTP_HOST e SMTP_FROM no arquivo privado do servidor.", "erro")
        elif not recipients:
            flash("Cadastre e ative ao menos um destinatário antes do teste.", "erro")
        else:
            ts = now().isoformat(timespec="seconds")
            body = (f"E-mail de teste do Painel Server10 ({socket.gethostname()}).\n"
                    f"Enviado em {fmt_dt(ts)} por {session['user']}.\n")
            try:
                mailer.send_mail(cfg, recipients, "[Painel Server10] E-mail de teste", body)
                db.set_setting(conn, "last_mail_ok", ts)
                db.set_setting(conn, "last_mail_error", "")
                flash(f"E-mail de teste enviado para {len(recipients)} destinatário(s).", "ok")
            except Exception as exc:  # noqa: BLE001
                error = mailer.describe_error(exc)
                db.set_setting(conn, "last_mail_error", f"{ts} — {error}")
                flash(f"Falha no envio: {error}", "erro")
            conn.commit()
        return redirect(url_for("admin"))

    @app.post("/admin/linha-base/<int:disk_id>")
    @login_required
    def accept_baseline(disk_id):
        if disk_id not in cfg.disk_ids:
            abort(404)
        conn = get_db()
        changed = alerts.accept_baseline(conn, disk_id, now().isoformat(timespec="seconds"), session["user"])
        conn.commit()
        flash(f"Linha de base do disco ID {disk_id} atualizada ({changed} contadores). "
              "O estado será reavaliado na próxima coleta.", "ok")
        return redirect(url_for("admin"))

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(405)
    @app.errorhandler(429)
    def http_error(err):
        messages = {400: "A requisição não pôde ser processada.", 404: "Esta página não existe.",
                    405: "Ação não permitida neste endereço.", 429: "Muitas requisições seguidas."}
        message = err.description if err.code == 400 and "CSRF" in str(err.description) else messages[err.code]
        return render_template("error.html", code=err.code, message=message), err.code

    return app
