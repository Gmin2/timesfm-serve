import sys

from timesfm_serve import db

db.init()
print(db.create_key(sys.argv[1] if len(sys.argv) > 1 else "dev"))
