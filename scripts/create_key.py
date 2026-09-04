import sys

from timesfm_serve import db

db.migrate()
name = sys.argv[1] if len(sys.argv) > 1 else "dev"
label = sys.argv[2] if len(sys.argv) > 2 else None
account_id = db.find_or_create_account(name)
print(db.create_key(account_id, label))
db.pool.close()
