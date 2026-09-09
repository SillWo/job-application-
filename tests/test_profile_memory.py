import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from backend.api.router import QuestionAnswer, answer_profile_question, pending_profile_questions
from backend.persistence.database import Base
from backend.persistence.models import (
    ApplicationPlanRecord,
    CandidateProfile,
    JobSession,
    Notification,
    ProfileMemory,
    SessionQuestion,
    Vacancy,
)
from backend.services.profile_memory import (
    collect_finished_sessions,
    collect_session_questions,
    load_profile_memory,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(CandidateProfile(id=1))
        session.commit()
        yield session
    engine.dispose()


def create_session(db, *, site="hh", status="COMPLETED", question="Какой у вас уровень английского?", context=None):
    item = JobSession(profile_id=1, adapter_id=site, status=status)
    db.add(item)
    db.flush()
    vacancy = Vacancy(session_id=item.id, source=site, url=f"https://{site}.ru/vacancy/{item.id}",
                      title="Разработчик", data=context or {}, state="NEEDS_REVIEW")
    db.add(vacancy)
    db.flush()
    db.add(ApplicationPlanRecord(vacancy_id=vacancy.id, data={
        "form_fields": {"q": {"label": question, "options": ["B1", "B2"]}},
        "unanswered_fields": {"q": "В резюме нет ответа"},
    }))
    db.flush()
    return item


def test_collect_only_terminal_and_idempotent_across_restart(db):
    item = create_session(db, status="RUNNING")
    assert collect_session_questions(db, item) == 0
    assert not pending_profile_questions(1, db)
    item.status = "STOPPED"
    assert collect_session_questions(db, item) == 1
    db.commit()
    assert collect_finished_sessions(db) == 0
    assert len(list(db.scalars(select(Notification).where(Notification.kind == "session_questions")))) == 1
    questions = pending_profile_questions(1, db)
    assert len(questions) == 1
    assert questions[0]["vacancy_url"] == f"https://hh.ru/vacancy/{item.id}"


def test_user_answer_is_shared_between_sites_and_hidden_from_public_prompts(db):
    create_session(db)
    create_session(db, site="zarplata")
    assert collect_finished_sessions(db) == 2
    questions = pending_profile_questions(1, db)
    answer_profile_question(1, questions[0]["id"], QuestionAnswer(answer="B2"), db)
    assert pending_profile_questions(1, db) == []
    assert load_profile_memory(db, 1)[0]["answer"] == "B2"
    assert load_profile_memory(db, 2) == []
    item = create_session(db, site="hirehi")
    assert collect_session_questions(db, item) == 0


def test_context_keeps_office_salary_separate_from_remote(db):
    create_session(db, question="Желаемая зарплата?", context={"work_format": "офис"})
    create_session(db, question="Желаемая зарплата?", context={"work_format": "удалённо"})
    collect_finished_sessions(db)
    questions = pending_profile_questions(1, db)
    answer_profile_question(1, questions[0]["id"], QuestionAnswer(answer="100000 RUB"), db)
    assert len(pending_profile_questions(1, db)) == 1


def test_skip_does_not_create_memory_and_other_profile_cannot_answer(db):
    create_session(db)
    collect_finished_sessions(db)
    question = pending_profile_questions(1, db)[0]
    with pytest.raises(HTTPException) as error:
        answer_profile_question(2, question["id"], QuestionAnswer(answer="B2"), db)
    assert error.value.status_code == 404
    answer_profile_question(1, question["id"], QuestionAnswer(skip=True), db)
    assert not load_profile_memory(db, 1)
    assert not pending_profile_questions(1, db)


def test_authentication_questions_cannot_be_saved(db):
    create_session(db, question="Введите пароль")
    collect_finished_sessions(db)
    question = pending_profile_questions(1, db)[0]
    assert question["can_answer"] is False
    with pytest.raises(HTTPException) as error:
        answer_profile_question(1, question["id"], QuestionAnswer(answer="test"), db)
    assert error.value.status_code == 422
    assert not list(db.scalars(select(ProfileMemory)))


def test_non_structured_adapter_questions_are_collected_without_plan(db):
    item = create_session(db, site="zarplata")
    vacancy = db.scalar(select(Vacancy).where(Vacancy.session_id == item.id))
    vacancy.data = {"application_unanswered_questions": ["Когда можете начать?"]}
    assert collect_session_questions(db, item) == 2
    assert len(list(db.scalars(select(SessionQuestion)))) == 2
