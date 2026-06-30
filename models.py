from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(100))
    email = Column(String(255), unique=True, index=True)
    password_hash = Column(String(255))
    plan = Column(String(50), default="free")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    targets = relationship("Target", back_populates="user")


class Target(Base):
    __tablename__ = "targets"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)

    repo = Column(String(255), index=True)
    language = Column(String(100))
    score = Column(Float)
    status = Column(String(50), default="New Target")
    best_issue = Column(String(100))

    stars = Column(String(50))
    forks = Column(String(50))
    open_issues = Column(String(50))

    merge_probability = Column(String(100))
    difficulty = Column(String(100))
    estimated_time = Column(String(100))
    pitch = Column(Text)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="targets")
