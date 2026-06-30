from sqlalchemy import Column
from sqlalchemy import Integer
from sqlalchemy import String
from sqlalchemy import DateTime
from sqlalchemy.sql import func

from database import Base


class User(Base):

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)

    name = Column(String(100))

    email = Column(String(255), unique=True)

    password_hash = Column(String(255))

    plan = Column(String(50), default="free")

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
from sqlalchemy import Text
from sqlalchemy import Float


class Target(Base):

    __tablename__ = "targets"

    id = Column(Integer, primary_key=True)

    repo = Column(String(255), index=True)

    language = Column(String(100))

    score = Column(Float)

    status = Column(String(50), default="New Target")

    best_issue = Column(String(100))

    merge_probability = Column(String(100))

    difficulty = Column(String(100))

    estimated_time = Column(String(100))

    pitch = Column(Text)

    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
