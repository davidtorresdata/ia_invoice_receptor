"""Engine + session factory construction."""

from sqlalchemy import Engine
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.config.settings import Settings


def create_engine(settings: Settings) -> Engine:
    """Build the application engine (psycopg3 driver expected in URL)."""
    return sa_create_engine(
        settings.database_url,
        pool_pre_ping=True,       # survive idle disconnects
        pool_recycle=1800,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """
    expire_on_commit=False keeps ORM objects usable after commit — use cases
    work with detached domain entities, so this avoids lazy-load surprises.
    """
    return sessionmaker(
        bind=engine,
        class_=Session,
        expire_on_commit=False,
        autoflush=False,
    )
