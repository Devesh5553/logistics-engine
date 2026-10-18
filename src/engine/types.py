from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class DecisionResult(BaseModel):
    allowed: bool = Field(..., description="Whether the action is permitted")
    deny_code: Optional[str] = Field(None, description="Machine-readable denial code")
    reasons: List[str] = Field(default_factory=list, description="Human-readable explanation reasons")
    grant_ids: List[str] = Field(default_factory=list, description="Grants that permitted or were evaluated")
    rule: str = Field(..., description="The decisive rule or policy that governed the decision")
    tier: str = Field("generic", description="Explanation tier: 'full' (admin/auditor) or 'generic' (staff/client)")
    approval_id: Optional[str] = Field(None, description="ID of approval consumed or validated")
    metadata: Dict[str, Any] = Field(default_factory=dict, description="Additional context or evaluation snapshots")


class UserEvaluationSummary(BaseModel):
    user_id: str
    username: str
    full_name: str
    allowed: bool
    deny_code: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)
    grant_ids: List[str] = Field(default_factory=list)
    rule: str


class ComparisonResult(BaseModel):
    job_id: str
    action: str
    at: str
    user_a: UserEvaluationSummary
    user_b: UserEvaluationSummary
    differential_summary: str = Field(..., description="Plain-English explanation of why User A can and User B cannot")
