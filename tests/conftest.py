import os

# Set dummy env vars before any app modules are imported so module-level
# clients (OpenAI, etc.) don't raise "missing credentials" errors in tests.
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used-in-tests")
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-chars-minimum-pad")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")

# Create all tables in the test database (CI has no finance_tracker.db or
# alembic migration step, so we bootstrap the schema from the ORM models).
from app.database import Base, engine  # noqa: E402
Base.metadata.create_all(engine)
