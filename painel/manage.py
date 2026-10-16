"""Utilitários administrativos de linha de comando.

    python -m painel.manage hash-password   # gera ADMIN_PASSWORD_HASH (pede a senha)
    python -m painel.manage gen-secret      # gera SECRET_KEY
    python -m painel.manage init-db         # cria o banco no DB_PATH configurado
"""
import getpass
import secrets
import sys

from werkzeug.security import generate_password_hash

from . import db
from .config import load_config


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    command = argv[0] if argv else ""
    if command == "hash-password":
        password = getpass.getpass("Nova senha administrativa: ")
        if len(password) < 10:
            print("Use ao menos 10 caracteres.", file=sys.stderr)
            return 1
        if getpass.getpass("Repita a senha: ") != password:
            print("As senhas não conferem.", file=sys.stderr)
            return 1
        print(generate_password_hash(password, method="scrypt"))
        return 0
    if command == "gen-secret":
        print(secrets.token_hex(32))
        return 0
    if command == "init-db":
        cfg = load_config()
        db.connect(cfg.db_path).close()
        print(f"Banco pronto em {cfg.db_path}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
