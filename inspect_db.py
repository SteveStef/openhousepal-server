import sqlite3


conn = sqlite3.connect("collections.db")
cur = conn.cursor()

tables = [
    row[0]
    for row in cur.execute(
        "select name from sqlite_master "
        "where type = 'table' and name not like 'sqlite_%' "
        "order by name"
    )
]

print("TABLES")
for table in tables:
    print(table)

print("\nROW_COUNTS")
for table in tables:
    count = cur.execute(f'select count(*) from "{table}"').fetchone()[0]
    print(f"{table}: {count}")

print("\nUSER_SCHEMA")
print(
    cur.execute(
        "select sql from sqlite_master where type = 'table' and name = 'users'"
    ).fetchone()[0]
)

print("\nALEMBIC_VERSION")
print(cur.execute("select * from alembic_version").fetchall())
