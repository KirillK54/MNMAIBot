import pytest
from pydantic import ValidationError

from vpi_bot.ai.schemas import JudgeResult


def test_judge_result_accepts_typed_structured_changes():
    result = JudgeResult.model_validate(
        {
            "is_game_action": True,
            "status": "partial_success",
            "title": "Промышленная реформа",
            "description": "Начат первый этап.",
            "reasoning": "Средств достаточно для ограниченной программы.",
            "positives": ["Начались работы"],
            "negatives": ["Сроки сдвинуты"],
            "conditions": ["Нужны специалисты"],
            "changes": {
                "metrics": {"treasury": -5000},
                "resources": {"Железо": -10},
                "research": [],
            },
            "duration_turns": 2,
        }
    )
    assert result.changes.metrics["treasury"] == -5000
    assert result.changes.resources["Железо"] == -10


def test_project_proposal_is_typed_and_bounded():
    result = JudgeResult.model_validate(
        {
            "is_game_action": True,
            "status": "success",
            "title": "Завод",
            "description": "Строительство одобрено.",
            "changes": {
                "metrics": {},
                "resources": {},
                "military": {
                    "metrics": {},
                    "units": {},
                    "equipment": {},
                    "ammunition": {},
                },
                "projects": [
                    {
                        "name": "Литейный завод",
                        "facility_type": "Металлургия",
                        "cost": 200,
                        "resource_costs": {"Железо": 5},
                        "production_per_tick": {"Сталь": 3},
                        "input_per_tick": {"Железо": 2},
                        "operating_cost": 10,
                        "interval_hours": 4,
                        "duration_turns": 3,
                        "required_technology": None,
                    }
                ],
            },
        }
    )
    assert result.changes.projects[0].duration_turns == 3
    assert result.changes.projects[0].resource_costs["Железо"] == 5
    project_data = result.changes.projects[0].model_dump()
    with pytest.raises(ValidationError):
        type(result.changes.projects[0]).model_validate(project_data | {"duration_turns": 1000})
    with pytest.raises(ValidationError):
        type(result.changes.projects[0]).model_validate(project_data | {"surprise": True})


def test_research_proposals_are_explicit_and_bounded():
    result = JudgeResult.model_validate(
        {
            "is_game_action": True,
            "status": "success",
            "title": "Разработка радиосвязи",
            "description": "Начата программа исследований.",
            "changes": {
                "research": [
                    {
                        "technology_name": "Радиосвязь",
                        "description": "Созданы лаборатории для изучения радиопередачи.",
                        "duration_turns": 3,
                    }
                ]
            },
        }
    )
    assert result.changes.research[0].technology_name == "Радиосвязь"
    assert result.changes.research[0].duration_turns == 3
    bad = result.changes.research[0].model_dump() | {"duration_turns": 6}
    with pytest.raises(ValidationError):
        type(result.changes.research[0]).model_validate(bad)


def test_judge_result_rejects_unknown_fields_and_statuses():
    with pytest.raises(ValidationError):
        JudgeResult.model_validate({"status": "everything_successful", "surprise": True})


def test_judge_result_rejects_unbounded_changes():
    with pytest.raises(ValidationError):
        JudgeResult.model_validate(
            {
                "is_game_action": True,
                "status": "success",
                "title": "Проверка",
                "description": "Текст",
                "changes": {"metrics": {"treasury": 1e14}, "resources": {}},
            }
        )
