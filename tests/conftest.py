import os

# Set dummy env vars before any app modules are imported so module-level
# clients (OpenAI, etc.) don't raise "missing credentials" errors in tests.
os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used-in-tests")
os.environ.setdefault("SECRET_KEY", "test-secret-key-32-chars-minimum-pad")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
