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

    paddle_customer_id = Column(String(255), nullable=True, index=True)
    paddle_subscription_id = Column(String(255), nullable=True, index=True)
    subscription_status = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    targets = relationship("Target", back_populates="user")


class Target(Base):
    """
    A saved/tracked repository analysis.

    This is the single source of truth for analyses, pipeline status, and
    analytics. analytics.csv / targets.csv are no longer written to or read
    from — everything lives here so it can be scoped per user.
    """

    __tablename__ = "targets"

    id = Column(Integer, primary_key=True)

    # Nullable so anonymous (logged-out) free-tier analyses can still be
    # recorded for rate limiting, but every pipeline entry created by a
    # logged-in user always has user_id set, and dashboard/pipeline queries
    # filter on it so accounts no longer share data.
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    repo = Column(String(255), index=True)
    repo_url = Column(String(500))
    language = Column(String(100))
    score = Column(Float)
    status = Column(String(50), default="New Target")
    best_issue = Column(String(100))
    best_issue_url = Column(String(500))
    merge_probability = Column(String(100))
    difficulty = Column(String(100))
    estimated_time = Column(String(100))
    pitch = Column(Text)

    stars = Column(Integer, default=0)
    forks = Column(Integer, default=0)
    open_issues = Column(Integer, default=0)

    # Used only for free-tier rate limiting of anonymous (logged-out) visitors.
    ip_address = Column(String(64), index=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)

    user = relationship("User", back_populates="targets")
