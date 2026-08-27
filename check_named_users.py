import sys

import gestalt
import gesp

client = gestalt.Gestalt(
    dbfile=sys.argv[1] if len(sys.argv) > 1 else gestalt.DEFAULT_DB
)
client.fetchall("select * from masks")
for mask in client.fetchall("select * from masks"):
    for member in gesp.Rules.from_json(mask["rules"]).named:
        if not client.fetchone("select 1 from users where userid = ?", (member,)):
            print(f"{mask['maskid']}: {member}")
