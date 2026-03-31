# -*- coding: utf-8 -*-
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

"""
Zapusti ODIN RAZ pered zapuskom novogo main.py:
    python migrate.py
"""
import sqlite3
import os

DB_PATH = "bot.db"

if not os.path.exists(DB_PATH):
    print("OK: bot.db ne najden - migratsiya ne nuzhna, novaya baza sozdastsa avtomaticheski.")
    exit(0)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

migrations = [
    ("chats", "username", "TEXT"),
    ("chats", "invite_link", "TEXT"),
]

for table, col, col_type in migrations:
    try:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")
        print(f"OK: dobavlena kolonka {table}.{col}")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print(f"SKIP: {table}.{col} uzhe suschestvuet")
        else:
            print(f"ERROR: {e}")

conn.commit()
conn.close()
print("\nMigratsiya zavershena. Teper zapuskaj main.py")