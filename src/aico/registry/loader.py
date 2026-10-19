"""Nạp thư mục company/ thành model và kiểm tra tham chiếu chéo.

Cấu hình sai thì từ chối khởi động: mọi lỗi được gom lại và báo một lần.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from aico.core import OrgSpec, PolicySpec, RoleSpec, SopSpec

REQUIRED_STEPS = {"fix_ticket": ("triage", "plan", "code", "review", "merge")}


class RegistryError(Exception):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Cấu hình công ty không hợp lệ:\n- " + "\n- ".join(problems))


@dataclass(frozen=True)
class Company:
    root: Path
    org: OrgSpec
    roles: Mapping[str, RoleSpec]
    sops: Mapping[str, SopSpec]
    policy: PolicySpec

    def role(self, role_id: str) -> RoleSpec:
        try:
            return self.roles[role_id]
        except KeyError:
            raise RegistryError([f"không có vai trò '{role_id}'"]) from None

    def sop(self, sop_id: str) -> SopSpec:
        try:
            return self.sops[sop_id]
        except KeyError:
            raise RegistryError([f"không có SOP '{sop_id}'"]) from None

    def prompt_text(self, role_id: str) -> str:
        role = self.role(role_id)
        if role.prompt is None:
            return ""
        return (self.root / role.prompt).read_text(encoding="utf-8")


def _read_yaml(path: Path, problems: list[str]) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        problems.append(f"{path.name}: không tìm thấy file")
    except yaml.YAMLError as exc:
        problems.append(f"{path.name}: YAML lỗi: {exc}")
    return None


def _parse[M: BaseModel](path: Path, model: type[M], problems: list[str], root: Path) -> M | None:
    data = _read_yaml(path, problems)
    if data is None:
        return None
    rel = path.relative_to(root).as_posix()
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(gốc)"
            problems.append(f"{rel}: {loc}: {err['msg']}")
    return None


def _load_dir[M: BaseModel](
    root: Path, sub: str, model: type[M], problems: list[str]
) -> dict[str, M]:
    items: dict[str, M] = {}
    for path in sorted((root / sub).glob("*.yaml")):
        spec = _parse(path, model, problems, root)
        if spec is None:
            continue
        spec_id = getattr(spec, "id")  # noqa: B009 - mọi spec trong thư mục đều có id
        if spec_id != path.stem:
            problems.append(f"{sub}/{path.name}: id '{spec_id}' phải trùng tên file")
            continue
        items[spec_id] = spec
    return items


def _cross_check(
    root: Path,
    org: OrgSpec,
    roles: dict[str, RoleSpec],
    sops: dict[str, SopSpec],
    policy: PolicySpec,
    problems: list[str],
) -> None:
    dept_ids = {d.id for d in org.departments}
    for dept in org.departments:
        head = roles.get(dept.head)
        if head is None:
            problems.append(
                f"org.yaml: phòng '{dept.id}' có trưởng phòng '{dept.head}' không tồn tại"
            )
        elif head.department != dept.id:
            problems.append(f"org.yaml: trưởng phòng '{dept.head}' không thuộc phòng '{dept.id}'")

    for role in roles.values():
        where = f"roles/{role.id}.yaml"
        if role.department not in dept_ids:
            problems.append(f"{where}: phòng ban '{role.department}' không có trong org.yaml")
        if role.reports_to is not None and role.reports_to not in roles:
            problems.append(f"{where}: reports_to '{role.reports_to}' không tồn tại")
        if role.worker in ("llm", "openhands") and not role.models():
            problems.append(f"{where}: worker '{role.worker}' cần khai báo model")
        if role.prompt is not None and not (root / role.prompt).is_file():
            problems.append(f"{where}: không tìm thấy prompt '{role.prompt}'")
        for grant in role.tools:
            for tool in grant.allow:
                level = policy.level_of(grant.server, tool)
                if level is None:
                    problems.append(
                        f"{where}: tool '{grant.server}.{tool}' chưa có mức rủi ro trong policy"
                    )
                elif level.rank > role.max_risk.rank:
                    problems.append(
                        f"{where}: tool '{grant.server}.{tool}' mức {level} vượt max_risk "
                        f"{role.max_risk}"
                    )

    for sop in sops.values():
        where = f"sops/{sop.id}.yaml"
        if sop.department not in dept_ids:
            problems.append(f"{where}: phòng ban '{sop.department}' không có trong org.yaml")
        missing = [s for s in REQUIRED_STEPS[sop.graph] if s not in sop.steps]
        if missing:
            problems.append(f"{where}: thiếu bước {', '.join(missing)}")
        for step, role_id in sop.steps.items():
            if role_id not in roles:
                problems.append(
                    f"{where}: bước '{step}' giao cho vai trò '{role_id}' không tồn tại"
                )


def load_company(root: str | Path) -> Company:
    root = Path(root)
    problems: list[str] = []
    org = _parse(root / "org.yaml", OrgSpec, problems, root)
    policy = _parse(root / "policies" / "default.yaml", PolicySpec, problems, root)
    roles = _load_dir(root, "roles", RoleSpec, problems)
    sops = _load_dir(root, "sops", SopSpec, problems)
    if org is not None and policy is not None:
        _cross_check(root, org, roles, sops, policy, problems)
    if problems or org is None or policy is None:
        raise RegistryError(problems)
    return Company(root=root, org=org, roles=roles, sops=sops, policy=policy)
