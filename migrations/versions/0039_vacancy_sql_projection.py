"""Materialize scalar vacancy/evaluation projections for SQL pagination."""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None

_SCORE_KEYS = ("tasks", "skills", "experience_depth", "role_match", "industry", "special_requirements")
_ALIASES = {"required_years": "experience_depth", "title": "role_match", "languages": "special_requirements"}
_BATCH_SIZE = 500


def _columns(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _json(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _numeric(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _evaluation_values(data, existing=None) -> dict:
    payload = _json(data)
    existing = existing or {}
    values = {"total_score": _numeric(payload.get("score"))}
    values.update({key: None for key in _SCORE_KEYS})
    breakdown = payload.get("score_breakdown")
    if isinstance(breakdown, list):
        rows = ((row.get("key"), row.get("points")) for row in breakdown if isinstance(row, dict))
    elif isinstance(breakdown, dict):
        rows = breakdown.items()
    else:
        rows = ()
    canonical_values = {}
    legacy_values = {}
    for raw_key, raw_value in rows:
        if not isinstance(raw_key, str):
            continue
        key = _ALIASES.get(raw_key)
        if key is not None:
            legacy_values[key] = _numeric(raw_value)
        elif raw_key in _SCORE_KEYS:
            canonical_values[raw_key] = _numeric(raw_value)
    for key in _SCORE_KEYS:
        values[key] = canonical_values.get(key, legacy_values.get(key))
        if values[key] is None:
            values[key] = _numeric(existing.get(key))
    if values["total_score"] is None:
        values["total_score"] = _numeric(existing.get("total_score"))
    values.update({
        "decision": payload.get("decision") if isinstance(payload.get("decision"), str) else existing.get("decision"),
        "confidence": _numeric(payload.get("confidence")) if payload.get("confidence") is not None else _numeric(existing.get("confidence")),
        "category": payload.get("category") if isinstance(payload.get("category"), str) else existing.get("category"),
    })
    return values


def _vacancy_values(row) -> dict:
    payload = _json(row["data"])
    parts = [row["external_id"] or "", row["title"] or "", row["company"] or ""]
    return {
        "search_text": " ".join(str(value) for value in parts if value).casefold(),
        "title_sort": (row["title"] or "").casefold(),
        "site_sort": (row["site"] or "").casefold(),
        "error_code": payload.get("error_code") or payload.get("outcome_code"),
        "error_message": payload.get("error_message") or payload.get("outcome_message"),
    }


def _create_index(name: str, table: str, columns) -> None:
    # Projection indexes may already exist in a development or historical
    # database with the old all-ASC definition.  Recreate by name so the
    # migration also repairs that schema instead of silently retaining an
    # index which cannot satisfy ``value DESC, id ASC`` without a temp sort.
    if name in _indexes(table):
        op.drop_index(name, table_name=table)
    op.create_index(name, table, columns)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("vacancies") or not inspector.has_table("evaluations"):
        return

    vacancy_columns = _columns("vacancies")
    with op.batch_alter_table("vacancies") as batch:
        for name, column in (
            ("search_text", sa.Column("search_text", sa.Text(), nullable=False, server_default="")),
            ("title_sort", sa.Column("title_sort", sa.Text(), nullable=False, server_default="")),
            ("site_sort", sa.Column("site_sort", sa.Text(), nullable=False, server_default="")),
            ("error_code", sa.Column("error_code", sa.String(255), nullable=True)),
            ("error_message", sa.Column("error_message", sa.Text(), nullable=True)),
        ):
            if name not in vacancy_columns:
                batch.add_column(column)

    evaluation_columns = _columns("evaluations")
    with op.batch_alter_table("evaluations") as batch:
        for name, column in (
            ("total_score", sa.Column("total_score", sa.Float(), nullable=True)),
            *( (key, sa.Column(key, sa.Float(), nullable=True)) for key in _SCORE_KEYS ),
            ("decision", sa.Column("decision", sa.String(50), nullable=True)),
            ("confidence", sa.Column("confidence", sa.Float(), nullable=True)),
            ("category", sa.Column("category", sa.String(100), nullable=True)),
        ):
            if name not in evaluation_columns:
                batch.add_column(column)

    # Decode and copy only scalar values in bounded batches.  This preserves
    # historical scores exactly and performs no model re-evaluation.
    last_id = 0
    while True:
        rows = bind.execute(sa.text(
            "SELECT id, external_id, title, company, site, data FROM vacancies "
            "WHERE id > :last_id ORDER BY id LIMIT :batch_size"
        ), {"last_id": last_id, "batch_size": _BATCH_SIZE}).mappings().all()
        if not rows:
            break
        bind.execute(sa.text(
            "UPDATE vacancies SET search_text=:search_text, title_sort=:title_sort, "
            "site_sort=:site_sort, error_code=:error_code, error_message=:error_message WHERE id=:id"
        ), [{**_vacancy_values(row), "id": row["id"]} for row in rows])
        last_id = rows[-1]["id"]

    last_id = 0
    while True:
        rows = bind.execute(sa.text(
            "SELECT id, data, total_score, tasks, skills, experience_depth, role_match, "
            "industry, special_requirements, decision, confidence, category "
            "FROM evaluations WHERE id > :last_id ORDER BY id LIMIT :batch_size"
        ), {"last_id": last_id, "batch_size": _BATCH_SIZE}).mappings().all()
        if not rows:
            break
        updates = [{**_evaluation_values(row["data"], row), "id": row["id"]} for row in rows]
        bind.execute(sa.text(
            "UPDATE evaluations SET total_score=:total_score, tasks=:tasks, skills=:skills, "
            "experience_depth=:experience_depth, role_match=:role_match, industry=:industry, "
            "special_requirements=:special_requirements, decision=:decision, confidence=:confidence, "
            "category=:category WHERE id=:id"
        ), updates)
        last_id = rows[-1]["id"]

    for name, table, columns in (
        ("ix_vacancies_projection_search", "vacancies", ["search_text"]),
        # The id suffix makes ordering deterministic for equal projected
        # values and lets the page-id query stop at LIMIT.
        ("ix_vacancies_projection_title", "vacancies", [sa.text("title_sort DESC"), sa.text("id ASC")]),
        ("ix_vacancies_projection_site", "vacancies", [sa.text("site_sort DESC"), sa.text("id ASC")]),
        ("ix_vacancies_projection_state", "vacancies", [sa.text("state DESC"), sa.text("id ASC")]),
        ("ix_vacancies_projection_date", "vacancies", [sa.text("status_changed_at DESC"), sa.text("id ASC")]),
        ("ix_evaluations_projection_total", "evaluations", [sa.text("total_score DESC"), sa.text("vacancy_id ASC")]),
        *(
            (f"ix_evaluations_projection_{key}", "evaluations", [sa.text(f"{key} DESC"), sa.text("vacancy_id ASC")])
            for key in _SCORE_KEYS
        ),
        ("ix_vacancies_projection_title_asc", "vacancies", [sa.text("title_sort ASC"), sa.text("id ASC")]),
        ("ix_vacancies_projection_site_asc", "vacancies", [sa.text("site_sort ASC"), sa.text("id ASC")]),
        ("ix_vacancies_projection_state_asc", "vacancies", [sa.text("state ASC"), sa.text("id ASC")]),
        ("ix_vacancies_projection_date_asc", "vacancies", [sa.text("status_changed_at ASC"), sa.text("id ASC")]),
        ("ix_evaluations_projection_total_asc", "evaluations", [sa.text("total_score ASC"), sa.text("vacancy_id ASC")]),
        *(
            (f"ix_evaluations_projection_{key}_asc", "evaluations", [sa.text(f"{key} ASC"), sa.text("vacancy_id ASC")])
            for key in _SCORE_KEYS
        ),
    ):
        _create_index(name, table, columns)

    # These names were used by an earlier, superseded projection attempt and
    # are not part of the ORM metadata.  Remove them while upgrading an
    # already materialised database so schema inspection remains consistent.
    for name, table in (
        ("ix_evaluations_projection_scores", "evaluations"),
        ("ix_vacancies_projection_state_date", "vacancies"),
    ):
        if name in _indexes(table):
            op.drop_index(name, table_name=table)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("evaluations"):
        for name in (
            "ix_evaluations_projection_total",
            *(f"ix_evaluations_projection_{key}" for key in _SCORE_KEYS),
            "ix_evaluations_projection_total_asc",
            *(f"ix_evaluations_projection_{key}_asc" for key in _SCORE_KEYS),
            # Older development databases may have the superseded composite
            # index; remove it when rolling this migration back as well.
            "ix_evaluations_projection_scores",
        ):
            if name in _indexes("evaluations"):
                op.drop_index(name, table_name="evaluations")
        columns = _columns("evaluations")
        with op.batch_alter_table("evaluations") as batch:
            for name in ("total_score", *_SCORE_KEYS, "decision", "confidence", "category"):
                if name in columns:
                    batch.drop_column(name)
    if inspector.has_table("vacancies"):
        for name in (
            "ix_vacancies_projection_search", "ix_vacancies_projection_title",
            "ix_vacancies_projection_site", "ix_vacancies_projection_state",
            "ix_vacancies_projection_date",
            "ix_vacancies_projection_title_asc", "ix_vacancies_projection_site_asc",
            "ix_vacancies_projection_state_asc", "ix_vacancies_projection_date_asc",
            "ix_vacancies_projection_state_date",
        ):
            if name in _indexes("vacancies"):
                op.drop_index(name, table_name="vacancies")
        columns = _columns("vacancies")
        with op.batch_alter_table("vacancies") as batch:
            for name in ("search_text", "title_sort", "site_sort", "error_code", "error_message"):
                if name in columns:
                    batch.drop_column(name)
