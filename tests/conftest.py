import os

# Load .env first so the opt-in live parser tests (RUN_LIVE_PARSER_TESTS=1) get the
# real OPENAI_API_KEY. Without this the setdefault below wins and they fail to auth.
# CI has no .env, so it falls through to the dummies as before.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# Set dummy env vars before any app modules are imported so module-level
# clients (OpenAI, etc.) don't raise "missing credentials" errors in tests.
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used-in-tests")
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-chars-minimum-pad")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")

# Create all tables in the test database (CI has no finance_tracker.db or
# alembic migration step, so we bootstrap the schema from the ORM models).
# app.models must be imported first so the ORM classes register with Base.metadata.
import app.models  # noqa: F401, E402
from app.database import Base, engine  # noqa: E402
Base.metadata.create_all(engine)
