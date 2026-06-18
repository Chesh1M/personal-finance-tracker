#!/usr/bin/env bash
set -e

pip install -r requirements.txt

# Alembic's default alembic_version table uses VARCHAR(32), which is too short
# for our revision IDs. Pre-create it with VARCHAR(64) before running migrations.
python - <<'EOF'
import os
from sqlalchemy import create_engine, inspect, text

url = os.environ.get("DATABASE_URL", "")
if url.startswith("postgres://"):
    url = url.replace("postgres://", "postgresql://", 1)

if url.startswith("postgresql://"):
    engine = create_engine(url)
    with engine.connect() as conn:
        if "alembic_version" not in inspect(engine).get_table_names():
            conn.execute(text(
                "CREATE TABLE alembic_version ("
                "version_num VARCHAR(64) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
                ")"
            ))
            conn.commit()
EOF

alembic upgrade head