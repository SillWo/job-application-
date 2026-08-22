ROLE_PROMPTS = {
    "hirehi_category": "Выбери одну категорию из payload.allowed_categories по резюме. Верни только JSON.",
    "job_summary": "Составь краткое описание вакансии только по title и description. Верни только JSON.",
    "resume_analyst": (
        "Верни ровно один JSON-объект ResumeAnalysis в корне. Не используй wrappers: analysis, resumes, "
        "candidate_name, result или data; не возвращай массив. Используй только title, tasks, industry, "
        "required_years, languages, skills. Для каждого верни score, confidence, evidence и explanation. "
        "Диапазоны: title 0..2, tasks 0..3, industry 0..4, required_years 0..2, languages 0..2, skills 0..3. "
        "Рубрика: A title: 2 — должность совпадает; B tasks: 3 — задачи совпадают; C industry: 4 — сфера совпадает и профиль задач совпадает (B2C/B2B). "
        "D required_years: оценивай именно минимально требуемые вакансией годы; полное соответствие — не более чем на один год. "
        "E languages: 2 — ровно на один уровень CEFR; F skills: полный балл, если отсутствует не более двух; ноль, если отсутствует более двух и они не смежны. "
        "Оценивай только явно подтверждённые факты из job и resumes. Evidence — короткие точные цитаты. "
        "Без подтверждения score=0 и evidence=[]. Не выдумывай стаж, навыки, языки или условия. "
        "Если иностранный язык не требуется, languages.score=2, languages.confidence=1 и languages.evidence=[]. "
        "Для всех остальных критериев и для явного требования иностранного языка score>0 требует confidence>0 и evidence."
    ),
    "writer": (
        "Напиши сопроводительное письмо не более 70 слов ровно в трёх коротких смысловых блоках: "
        "почему вакансия, почему компания, преимущества кандидата. Используй только profile, resumes и job. "
        "Не выдумывай факты, контакты, навыки, цифры или достижения. Финальную фразу добавляет код."
    ),
    "profile": (
        "Строго распарсь резюме HH в один JSON-объект ResumeImportData с ключами profile и resume. "
        "Заполни все найденные поля схемы, сохрани все записи образования, опыта, языков и навыков. "
        "Не добавляй markdown, facts, verified_facts или другие ключи. Не выдумывай данные; отсутствующие "
        "nullable-поля — null, массивы — []. Даты сохраняй как в источнике, контакты не помещай в about или skills."
    ),
}

ROLE_OPTIONS = {
    "hirehi_category": {"temperature": 0, "num_predict": 120, "think": False},
    "job_summary": {"temperature": 0, "num_predict": 180, "think": False},
    "resume_analyst": {"temperature": 0.1, "num_ctx": 32768, "num_predict": 5000, "think": False},
    "writer": {"temperature": 0.3, "num_ctx": 32768, "num_predict": 500, "think": False},
    "profile": {"temperature": 0, "num_ctx": 32768, "num_predict": 12000, "think": False},
}
