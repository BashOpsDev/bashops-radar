from sqlalchemy import JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
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

    email_verified = Column(Boolean, default=False, nullable=False)
    email_verification_token = Column(String(255), nullable=True, index=True)
    email_verification_sent_at = Column(DateTime(timezone=True), nullable=True)
    password_reset_token = Column(String(255), nullable=True, index=True)
    password_reset_sent_at = Column(DateTime(timezone=True), nullable=True)
    marketing_opt_in = Column(Boolean, default=False, nullable=False)
    marketing_opt_in_at = Column(DateTime(timezone=True), nullable=True)
    github_id = Column(String(255), nullable=True, index=True)
    github_username = Column(String(255), nullable=True)
    auth_provider = Column(String(50), default="email")

    paddle_customer_id = Column(String(255), nullable=True, index=True)
    paddle_subscription_id = Column(String(255), nullable=True, index=True)
    subscription_status = Column(String(50), nullable=True)
    maintainer_pilot_access = Column(Boolean, default=False, nullable=False)
    maintainer_paddle_subscription_id = Column(String(255), nullable=True, index=True)
    maintainer_subscription_status = Column(String(50), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    targets = relationship("Target", back_populates="user")
    maintainer_analyses = relationship("MaintainerAnalysis", back_populates="user")
    developer_profiles = relationship("DeveloperProfile", back_populates="user")


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


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_name = Column(String(100), nullable=False, index=True)
    page = Column(String(500), nullable=True)
    referrer = Column(String(500), nullable=True)
    user_agent = Column(String(500), nullable=True)
    metadata_json = Column("metadata", Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class MaintainerAnalysis(Base):
    __tablename__ = "maintainer_analyses"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    repository_full_name = Column(String(255), nullable=False, index=True)
    repository_url = Column(String(500), nullable=False)
    status = Column(String(50), nullable=False, default="completed")
    analyzed_issue_count = Column(Integer, nullable=False)
    report_json = Column(Text, nullable=False)
    is_partial = Column(Boolean, nullable=False, default=False)
    error_code = Column(String(100), nullable=True)
    plan_context = Column(String(50), nullable=False)
    analysis_version = Column(String(50), nullable=False)
    ip_address = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", back_populates="maintainer_analyses")


class DeveloperProfile(Base):
    __tablename__ = "developer_profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, unique=True, index=True)
    github_username = Column(String(39), nullable=False, unique=True, index=True)
    github_user_id = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=False)
    avatar_url = Column(String(500), nullable=True)
    bio = Column(Text, nullable=True)
    public_location = Column(String(255), nullable=True)
    profile_url = Column(String(500), nullable=False)
    profile_data = Column(JSON, nullable=False)
    strength_data = Column(JSON, nullable=False)
    contribution_data = Column(JSON, nullable=False)
    analyzed_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    is_claimed = Column(Boolean, nullable=False, default=False)
    is_public = Column(Boolean, nullable=False, default=False)
    public_slug = Column(String(80), nullable=False, unique=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="developer_profiles")
