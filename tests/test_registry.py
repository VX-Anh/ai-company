from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aico.core import ActionLevel
from aico.registry import Company, RegistryError, load_company


def _edit(path: Path, **changes: object) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_sample_company_loads(company: Company) -> None:
    coder = company.role("coder")
    assert coder.worker == "openhands"
    assert coder.max_risk is ActionLevel.L2
    grant = coder.grant_for("gitlab")
    assert grant is not None
    assert grant.constraints.branch_pattern == "ai/*"
    assert company.sop("fix_ticket").steps["merge"] == "merge_bot"
    assert "Coder" in company.prompt_text("coder")


def test_unknown_field_is_rejected(company_copy: Path) -> None:
    _edit(company_copy / "roles" / "coder.yaml", maxrisk="L2")
    with pytest.raises(RegistryError) as exc:
        load_company(company_copy)
    assert any("maxrisk" in p for p in exc.value.problems)


def test_tool_above_max_risk_is_rejected(company_copy: Path) -> None:
    path = company_copy / "roles" / "coder.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["tools"][0]["allow"].append("merge_mr")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError) as exc:
        load_company(company_copy)
    assert any("merge_mr" in p and "vượt max_risk" in p for p in exc.value.problems)


def test_tool_without_policy_level_is_rejected(company_copy: Path) -> None:
    path = company_copy / "roles" / "tech_lead.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["tools"][0]["allow"].append("delete_repo")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError) as exc:
        load_company(company_copy)
    assert any("gitlab.delete_repo" in p for p in exc.value.problems)


def test_broken_references_are_all_reported(company_copy: Path) -> None:
    _edit(company_copy / "roles" / "reviewer.yaml", reports_to="nobody", department="sales")
    _edit(company_copy / "sops" / "fix_ticket.yaml", steps={"triage": "chief_of_staff"})
    with pytest.raises(RegistryError) as exc:
        load_company(company_copy)
    problems = "\n".join(exc.value.problems)
    assert "reports_to 'nobody'" in problems
    assert "phòng ban 'sales'" in problems
    assert "thiếu bước" in problems


def test_file_name_must_match_id(company_copy: Path) -> None:
    (company_copy / "roles" / "coder.yaml").rename(company_copy / "roles" / "dev.yaml")
    with pytest.raises(RegistryError) as exc:
        load_company(company_copy)
    assert any("phải trùng tên file" in p for p in exc.value.problems)


def test_invalid_yaml_is_reported(company_copy: Path) -> None:
    (company_copy / "org.yaml").write_text("company: [unclosed", encoding="utf-8")
    with pytest.raises(RegistryError) as exc:
        load_company(company_copy)
    assert any("org.yaml" in p and "YAML lỗi" in p for p in exc.value.problems)
