import os

# Prevent db.py from raising KeyError at import time in unit tests.
# Actual DB connections are mocked; this value is never used by the tests.
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5434/aigraph")
