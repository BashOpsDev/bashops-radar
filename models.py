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
