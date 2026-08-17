"""Replace candidate facts with editable profile and resume entities.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


PROFILE_COLUMNS = {
    "full_name": sa.Column("full_name", sa.String(length=255), nullable=True),
    "residence": sa.Column("residence", sa.String(length=255), nullable=True),
    "job_search_locations": sa.Column("job_search_locations", sa.JSON(), nullable=True),
    "contacts": sa.Column("contacts", sa.JSON(), nullable=True),
    "education": sa.Column("education", sa.JSON(), nullable=True),
    "languages": sa.Column("languages", sa.JSON(), nullable=True),
    "driver_license": sa.Column("driver_license", sa.Boolean(), nullable=True),
    "updated_at": sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
}


def _columns(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _as_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: object) -> list:
    if isinstance(value, list):
        return value
    return []


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    result = []
    for item in _as_list(value):
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            name = item.get("name") or item.get("value")
            if name and str(name).strip():
                result.append(str(name).strip())
    return result


def _period_dates(period: object) -> tuple[str | None, str | None]:
    if not isinstance(period, str) or not period.strip():
        return None, None
    value = " ".join(period.split())
    parts = re.split(r"\s+[—–]\s+|\s+-\s+", value, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip() or None, parts[1].strip() or None
    return value, None


def _legacy_experiences(value: object) -> list[dict]:
    result = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        start_date = item.get("start_date")
        end_date = item.get("end_date")
        if not start_date and not end_date:
            start_date, end_date = _period_dates(item.get("period"))
        result.append(
            {
                "company": str(item.get("company") or ""),
                "position": str(item.get("position") or item.get("role") or ""),
                "start_date": str(start_date) if start_date else None,
                "end_date": str(end_date) if end_date else None,
                "duties": str(item.get("duties") or item.get("description") or ""),
            }
        )
    return result


def _profile_backfill(data: dict) -> dict:
    locations = _strings(data.get("job_search_locations") or data.get("locations"))
    residence = data.get("residence") or data.get("location")
    if not residence and locations:
        residence = locations[0]
    contacts = {} if not isinstance(data.get("contacts"), dict) else dict(data["contacts"])
    for key in ("phone", "email", "messengers"):
        if not contacts.get(key) and data.get(key):
            contacts[key] = data[key]
    contacts.setdefault("messengers", [])
    return {
        "full_name": data.get("full_name"),
        "residence": residence,
        "job_search_locations": locations,
        "contacts": contacts,
        "education": _as_list(data.get("education")),
        "languages": _as_list(data.get("languages")),
        "driver_license": data.get("driver_license"),
    }


def _resume_backfill(profile: dict, legacy: dict) -> dict:
    filename = profile.get("filename")
    resume_path = profile.get("resume_path")
    desired_title = legacy.get("desired_title")
    return {
        "profile_id": profile["id"],
        "name": filename or desired_title or "Резюме",
        "desired_title": desired_title,
        "desired_salary": legacy.get("desired_salary"),
        "employment_types": _strings(legacy.get("employment_types")),
        "work_formats": _strings(legacy.get("work_formats")),
        "business_trips": legacy.get("business_trips"),
        "experiences": _legacy_experiences(legacy.get("experiences")),
        "skills": _strings(legacy.get("skills")),
        "about": str(legacy.get("about") or legacy.get("summary") or ""),
        "selected_for_matching": True,
        "original_filename": filename,
        "original_path": resume_path,
    }


def _backfill_legacy_profiles(bind) -> None:
    metadata = sa.MetaData()
    profiles = sa.Table("candidate_profiles", metadata, autoload_with=bind)
    resumes = sa.Table("resumes", metadata, autoload_with=bind)
    rows = bind.execute(sa.select(profiles)).mappings()
    for profile in rows:
        legacy = _as_dict(profile.get("data"))
        profile_values = _profile_backfill(legacy)
        updates = {
            key: value
            for key, value in profile_values.items()
            if profile.get(key) in (None, "", [], {}) and value not in (None, "", [], {})
        }
        if updates:
            bind.execute(
                sa.update(profiles).where(profiles.c.id == profile["id"]).values(**updates)
            )

        resume_values = _resume_backfill(profile, legacy)
        duplicate = bind.execute(
            sa.select(resumes.c.id)
            .where(
                resumes.c.profile_id == profile["id"],
                sa.or_(
                    sa.and_(
                        resumes.c.original_path.is_not(None),
                        resumes.c.original_path == resume_values["original_path"],
                    ),
                    sa.and_(
                        resumes.c.original_path.is_(None),
                        resumes.c.original_filename == resume_values["original_filename"],
                    ),
                ),
            )
            .limit(1)
        ).first()
        if duplicate:
            continue
        if resume_values["original_path"] is None and resume_values["original_filename"] is None:
            existing_resume = bind.execute(
                sa.select(resumes.c.id).where(resumes.c.profile_id == profile["id"]).limit(1)
            ).first()
            if existing_resume:
                continue
        now = datetime.now(timezone.utc)
        resume_values.update(created_at=now, updated_at=now)
        bind.execute(sa.insert(resumes).values(**resume_values))


def upgrade() -> None:
    bind = op.get_bind()
    existing = _columns("candidate_profiles")
    for name, column in PROFILE_COLUMNS.items():
        if name not in existing:
            op.add_column("candidate_profiles", column)

    if "resumes" not in sa.inspect(bind).get_table_names():
        op.create_table(
            "resumes",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("profile_id", sa.Integer(), sa.ForeignKey("candidate_profiles.id"), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False, server_default="Резюме"),
            sa.Column("desired_title", sa.String(length=255), nullable=True),
            sa.Column("desired_salary", sa.String(length=255), nullable=True),
            sa.Column("employment_types", sa.JSON(), nullable=True),
            sa.Column("work_formats", sa.JSON(), nullable=True),
            sa.Column("business_trips", sa.Boolean(), nullable=True),
            sa.Column("experiences", sa.JSON(), nullable=True),
            sa.Column("skills", sa.JSON(), nullable=True),
            sa.Column("about", sa.Text(), nullable=False, server_default=""),
            sa.Column("selected_for_matching", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("original_filename", sa.String(length=255), nullable=True),
            sa.Column("original_path", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_resumes_profile_id", "resumes", ["profile_id"])

    _backfill_legacy_profiles(bind)

    # Candidate facts are intentionally not copied. They are untrusted legacy
    # extraction artifacts and are outside the new profile contract.
    if "candidate_facts" in sa.inspect(bind).get_table_names():
        op.drop_table("candidate_facts")


def downgrade() -> None:
    bind = op.get_bind()
    if "resumes" in sa.inspect(bind).get_table_names():
        op.drop_index("ix_resumes_profile_id", table_name="resumes")
        op.drop_table("resumes")
    existing = _columns("candidate_profiles")
    for name in reversed(tuple(PROFILE_COLUMNS)):
        if name in existing:
            op.drop_column("candidate_profiles", name)
