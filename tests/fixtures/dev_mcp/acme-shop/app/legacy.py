"""Old reporting job. Kept until the nightly export is moved."""

from sqlalchemy import create_engine

# A connection string that someone wrote straight into the code.
REPORT_DSN = "postgresql+psycopg://report_ro:__CANARY_DSN_PASSWORD__@reports.internal.example.com/shop"


def report_engine():
    return create_engine(REPORT_DSN)
