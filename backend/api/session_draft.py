from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.persistence.database import get_db
from backend.persistence.models import JobSession, SessionFormDraft

router = APIRouter(prefix="/api/session-draft")
Level = Literal["low", "medium", "high", "maximum"]


class Influence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: Level = "medium"
    skills: Literal["low", "medium"] = "low"
    experience_depth: Level = "medium"
    role_match: Level = "medium"
    industry: Level = "medium"


class LaunchDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    adapter: Literal["hh", "hirehi", "zarplata"] = "hh"
    # A draft may contain an empty/unfinished limit while the user is typing.
    applicationLimit: str = Field(default="5", max_length=32)
    desiredJobDescription: str = Field(default="", max_length=2000)
    unlimitedApplications: bool = False
    influence: Influence = Field(default_factory=Influence)


class DraftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    draft: LaunchDraft


@router.get("")
def read_draft(db: Session = Depends(get_db)) -> dict:
    saved = db.get(SessionFormDraft, 1)
    if saved:
        return {"revision": saved.revision, "draft": saved.draft}
    # One-time recovery for installations predating the shared draft. An explicitly
    # saved empty description is authoritative and never resurrects old text.
    previous = db.scalar(select(JobSession).where(
        JobSession.desired_job_description != ""
    ).order_by(JobSession.id.desc()).limit(1))
    if previous is None:
        return {"revision": 0, "draft": None}
    draft = LaunchDraft(
        adapter=previous.adapter_id,
        desiredJobDescription=previous.desired_job_description,
        applicationLimit=str(previous.application_limit or 5),
        unlimitedApplications=previous.application_limit is None,
    )
    levels = ("low", "medium", "high", "maximum")
    for key, score in (previous.minimum_scores or {}).items():
        if key in Influence.model_fields and isinstance(score, int) and 1 <= score <= 4:
            setattr(draft.influence, key, levels[min(score, 2) - 1] if key == "skills" else levels[score - 1])
    return {"revision": 0, "draft": draft.model_dump()}


@router.put("")
def save_draft(payload: DraftUpdate, db: Session = Depends(get_db)) -> dict:
    value = payload.draft.model_dump()
    revision = payload.revision + 1
    if payload.revision == 0:
        db.add(SessionFormDraft(id=1, revision=revision, draft=value))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "Форма уже изменена в другой вкладке") from None
    else:
        result = db.execute(update(SessionFormDraft).where(
            SessionFormDraft.id == 1, SessionFormDraft.revision == payload.revision
        ).values(revision=revision, draft=value))
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "Форма уже изменена в другой вкладке")
        db.commit()
    return {"revision": revision, "draft": value}
