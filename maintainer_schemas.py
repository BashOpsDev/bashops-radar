from typing import Literal

from pydantic import BaseModel, Field


Confidence = Literal["High", "Medium", "Low"]
Priority = Literal["High", "Medium", "Low", "Needs Manual Review"]
Category = Literal[
    "Bug",
    "Feature Request",
    "Documentation",
    "Question/Support",
    "Testing/CI",
    "Performance",
    "Security",
    "Maintenance/Refactor",
    "Contributor Task",
    "Other",
]
ContributorSuitability = Literal[
    "Good first contribution",
    "Suitable for experienced contributor",
    "Maintainer-only/context-heavy",
    "Needs clarification first",
    "Not enough information",
]


class SuggestedLabel(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    reason: str = Field(min_length=1, max_length=300)
    confidence: Confidence


class DuplicateCandidate(BaseModel):
    issue_number: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=300)
    confidence: Confidence


class IssueTriageResult(BaseModel):
    number: int = Field(gt=0)
    suggested_category: Category
    suggested_labels: list[SuggestedLabel] = Field(default_factory=list, max_length=5)
    confidence: Confidence
    estimated_priority: Priority
    missing_information: list[str] = Field(default_factory=list, max_length=10)
    contributor_suitability: ContributorSuitability
    possible_duplicates: list[DuplicateCandidate] = Field(default_factory=list, max_length=5)
    suggested_first_response: str = Field(min_length=1, max_length=1200)


class MaintainerAIOutput(BaseModel):
    issues: list[IssueTriageResult] = Field(min_length=1, max_length=30)


class RepositorySummary(BaseModel):
    full_name: str
    url: str
    description: str
    stars: int = Field(ge=0)
    open_issues: int = Field(ge=0)


class ReportCounts(BaseModel):
    high_priority: int = Field(ge=0)
    possible_duplicates: int = Field(ge=0)
    missing_information: int = Field(ge=0)
    contributor_ready: int = Field(ge=0)
    needs_manual_review: int = Field(ge=0)


class ReportIssue(IssueTriageResult):
    title: str
    url: str
    current_labels: list[str] = Field(default_factory=list)


class MaintainerReport(BaseModel):
    schema_version: str
    analysis_version: str
    repository: RepositorySummary
    analyzed_at: str
    issues_reviewed: int = Field(ge=1, le=30)
    counts: ReportCounts
    summary: str
    issues: list[ReportIssue] = Field(min_length=1, max_length=30)
    disclaimer: str
    is_partial: bool
