"""Faz uma cópia de segurança do banco de dados, pronta pra baixar.

Uso, no console Bash, dentro da pasta jardim-app:

    python backup.py

A cópia vai pra pasta backups/, com a data e a hora no nome. Pode rodar com o site no ar: o SQLite
copia tudo de forma consistente, inclusive as alterações mais recentes, que no modo WAL ficam num
arquivo separado (jardim.db-wal) e ficariam de fora se você baixasse só o jardim.db.
Rode antes de cada atualização: o banco só é atualizado no Reload.
"""
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "instance" / "jardim.db"
TARGET_DIR = HERE / "backups"


def main():
    if not SOURCE.exists():
        print(f"Não achei o banco em {SOURCE}. Rode este comando dentro da pasta jardim-app.")
        return 1
    TARGET_DIR.mkdir(exist_ok=True)
    target = TARGET_DIR / f"jardim-{datetime.now():%Y-%m-%d-%H%M%S}.db"
    source, copy = sqlite3.connect(SOURCE), sqlite3.connect(target)
    with copy:
        source.backup(copy)  # cópia consistente, com tudo que já foi salvo (inclusive o que está no -wal)
    copy.close()
    source.close()
    print(f"Cópia salva: backups/{target.name} ({target.stat().st_size / 1024:.0f} KB)")
    print("Pra guardar no seu computador: aba Files > jardim-app > backups > ícone de download.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
