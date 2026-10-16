"""Envio de e-mail via SMTP. As credenciais vêm somente da configuração privada."""
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def smtp_configured(cfg):
    return bool(cfg.smtp_host and cfg.smtp_from)


def send_mail(cfg, recipients, subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.smtp_from
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="painel-server10")
    msg.set_content(body)

    context = ssl.create_default_context()
    if cfg.smtp_ssl:
        client = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=context)
    else:
        client = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
    with client:
        if not cfg.smtp_ssl and cfg.smtp_starttls:
            client.starttls(context=context)
        if cfg.smtp_user:
            client.login(cfg.smtp_user, cfg.smtp_password)
        client.send_message(msg)


def describe_error(exc):
    """Mensagem curta para exibir/logar, sem dados de autenticação."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Falha de autenticação no servidor SMTP"
    text = str(exc).replace("\n", " ").strip()
    return f"{type(exc).__name__}: {text[:200]}" if text else type(exc).__name__
