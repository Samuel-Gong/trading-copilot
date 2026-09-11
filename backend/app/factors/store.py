"""自定义/复合因子存储 (P3) — data/user_data/custom_factors/*.json。

镜像 custom_signals 的持久化写法; 单文件损坏只禁用该因子并告警, 不影响启动
(对齐 CONTRIBUTING 第 4 节插件隔离要求)。生命周期状态: draft → active →
watch → retired (P4 状态机, 存储字段就绪, 迁移逻辑见巡检设计)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from app.factors.dsl import compile_formula
from app.factors.registry import (
    FactorSpec,
    dynamic_factor_specs,
    factor_dependencies,
    get_factor,
    register_factor,
    registry_transaction,
    replace_dynamic_factors,
    unregister_factor,
)
from app.services.definition_transactions import definitions_transaction

logger = logging.getLogger(__name__)

CUSTOM_ID_PATTERN = re.compile(r"^uf_[a-z0-9_]{1,40}$")
COMPOSITE_ID_PATTERN = re.compile(r"^cf_[a-z0-9_]{1,40}$")
MAX_COMPOSITE_MEMBERS = 8
STATUSES = frozenset({"draft", "active", "watch", "retired"})


def _assert_acyclic(factor_id: str, references: set[str]) -> None:
    """以候选定义覆盖当前同 id 节点后检查整张引用图。"""
    visiting: set[str] = set()
    visited: set[str] = set()

    def children(current: str) -> set[str]:
        if current == factor_id:
            return references
        spec = get_factor(current)
        if spec is None:
            return set()
        if spec.kind == "composite":
            return {member_id for member_id, _weight in spec.components}
        if spec.kind == "custom":
            compiled = compile_formula(spec.formula_text)
            return set(compiled.referenced_factors) if compiled.ok else set()
        return set()

    def visit(current: str) -> None:
        if current in visiting:
            raise ValueError("因子定义存在循环引用")
        if current in visited:
            return
        visiting.add(current)
        for child in children(current):
            visit(child)
        visiting.remove(current)
        visited.add(current)

    visit(factor_id)


def _derived_scope(member_ids: set[str]) -> tuple[frozenset[str], bool, str]:
    """由引用因子交集派生资产范围与 PIT 元数据。"""
    asset_types = {"stock", "etf"}
    pit_sources: set[str] = set()
    for member_id in member_ids:
        member = get_factor(member_id)
        if member is None:
            continue
        asset_types.intersection_update(member.asset_types)
        if member.pit:
            pit_sources.add(member.pit_source)
    if not asset_types:
        raise ValueError("引用因子的适用资产类型没有交集")
    pit_sources.discard("none")
    pit_source = (
        "none"
        if not pit_sources
        else next(iter(pit_sources))
        if len(pit_sources) == 1
        else "mixed_announce"
    )
    return frozenset(asset_types), bool(pit_sources), pit_source


def _dir(data_dir: Path) -> Path:
    directory = data_dir / "user_data" / "custom_factors"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _path(data_dir: Path, factor_id: str) -> Path:
    return _dir(data_dir) / f"{factor_id}.json"


def load_all(data_dir: Path) -> list[dict]:
    """读取全部自定义/复合因子定义; 损坏文件跳过。"""
    out: list[dict] = []
    for file in sorted(_dir(data_dir).glob("*.json")):
        try:
            out.append(json.loads(file.read_text(encoding="utf-8")))
        except Exception as exc:
            logger.warning("custom factor load failed %s: %s", file.name, exc)
    return out


def save_one(data_dir: Path, definition: dict) -> None:
    """原子保存单个定义; 写入失败时保留旧文件。"""
    with definitions_transaction(data_dir):
        target = _path(data_dir, str(definition["id"]))
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(definition, ensure_ascii=False, indent=2)
        fd, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, target)
        finally:
            if fd >= 0:
                os.close(fd)
            temporary_path.unlink(missing_ok=True)


def exists(data_dir: Path, factor_id: str) -> bool:
    """定义文件是否存在, 不产生任何副作用。"""
    return _path(data_dir, factor_id).is_file()


def delete_one(data_dir: Path, factor_id: str) -> bool:
    with definitions_transaction(data_dir):
        target = _path(data_dir, factor_id)
        if target.exists():
            target.unlink()
            return True
        return False


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def to_spec(definition: dict) -> FactorSpec:
    """定义 → FactorSpec; 校验失败抛 ValueError (调用方 fail-closed)。

    custom: 依赖/预热由 DSL 编译推导 (编译失败即拒绝注册)。
    composite: 依赖 = 成员递归展开; 预热 = 成员最大值; 循环引用拒绝。
    """
    kind = str(definition.get("kind", "custom"))
    factor_id = str(definition.get("id", ""))
    label = str(definition.get("label", "")).strip()
    if not label:
        raise ValueError("label 不能为空")
    pattern = COMPOSITE_ID_PATTERN if kind == "composite" else CUSTOM_ID_PATTERN
    if not pattern.match(factor_id):
        raise ValueError(f"id 必须匹配 {pattern.pattern}")
    status = str(definition.get("status", "draft"))
    if status not in STATUSES:
        raise ValueError(f"status 必须是 {sorted(STATUSES)} 之一")

    if kind == "custom":
        formula = str(definition.get("formula", ""))
        compiled = compile_formula(formula)
        if not compiled.ok:
            first = compiled.errors[0]
            raise ValueError(f"公式无效 [{first.code}]: {first.message}")
        _assert_acyclic(factor_id, set(compiled.referenced_factors))
        asset_types, pit, pit_source = _derived_scope(
            set(compiled.referenced_factors)
        )
        return FactorSpec(
            id=factor_id,
            label=label,
            group=str(definition.get("group", "自定义")),
            formula_text=formula,
            kind="custom",
            version=int(definition.get("version", 1)),
            dependencies=frozenset(compiled.dependencies),
            warmup_bars=compiled.warmup_bars,
            direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
            asset_types=asset_types,
            pit=pit,
            pit_source=pit_source,  # type: ignore[arg-type]
            stability="stable" if status == "active" else "experimental",
        )

    if kind != "composite":
        raise ValueError(f"未知 kind: {kind}")
    members_raw = definition.get("members")
    if not isinstance(members_raw, dict) or not (2 <= len(members_raw) <= MAX_COMPOSITE_MEMBERS):
        raise ValueError(f"composite 成员必须是 {2}~{MAX_COMPOSITE_MEMBERS} 个")
    from app.factors.dsl import BASE_COLUMNS

    components: list[tuple[str, float]] = []
    for member_id, weight in members_raw.items():
        member_id = str(member_id)
        if member_id == factor_id:
            raise ValueError("composite 不能引用自身")
        try:
            weight = float(weight)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"成员 {member_id} 权重必须是数字") from exc
        if not weight:
            raise ValueError(f"成员 {member_id} 权重不能为 0")
        # 成员 = 注册表因子 或 enriched 基准列 (已物化, 可直接参与组合)
        if get_factor(member_id) is None and member_id not in BASE_COLUMNS:
            raise ValueError(f"未知成员因子: {member_id}")
        components.append((member_id, weight))
    # 环检测使用当前递归路径, 允许不同分支共享同一基础成员。
    def check_cycle(current: str, path: frozenset[str]) -> None:
        if current in path:
            raise ValueError("composite 成员存在循环引用")
        current_spec = get_factor(current)
        if current_spec is None or current_spec.kind != "composite":
            return
        next_path = path | {current}
        for child_id, _weight in current_spec.components:
            check_cycle(child_id, next_path)

    for member_id, _weight in components:
        check_cycle(member_id, frozenset({factor_id}))
    _assert_acyclic(factor_id, {member_id for member_id, _weight in components})
    dependencies = factor_dependencies([member_id for member_id, _ in components])
    warmup = max(
        ((get_factor(member_id).warmup_bars if get_factor(member_id) else 1) for member_id, _ in components),
        default=1,
    )
    formula_text = " + ".join(
        f"{weight:g}*zscore({member_id})" for member_id, weight in components
    )
    asset_types, pit, pit_source = _derived_scope(
        {member_id for member_id, _weight in components}
    )
    return FactorSpec(
        id=factor_id,
        label=label,
        group=str(definition.get("group", "组合")),
        formula_text=formula_text,
        kind="composite",
        version=int(definition.get("version", 1)),
        dependencies=dependencies,
        warmup_bars=warmup,
        direction=str(definition.get("direction", "none")),  # type: ignore[arg-type]
        asset_types=asset_types,
        pit=pit,
        pit_source=pit_source,  # type: ignore[arg-type]
        components=tuple(components),
        stability="stable" if status == "active" else "experimental",
    )


def register_definition(definition: dict) -> FactorSpec:
    """定义 → spec → 注册 (重复 id 版本未升时由注册表拒绝)。"""
    spec = to_spec(definition)
    register_factor(spec)
    return spec


def persist_definition(
    data_dir: Path,
    definition: dict,
    *,
    replace_registered: bool = False,
) -> FactorSpec:
    """原子落盘并重建传递依赖元数据; 失败时恢复完整旧状态。"""
    factor_id = str(definition["id"])
    with definitions_transaction(data_dir), registry_transaction():
        spec = to_spec(definition)
        target = _path(data_dir, factor_id)
        previous = target.read_bytes() if target.exists() else None
        previous_registry = dynamic_factor_specs()
        save_one(data_dir, definition)

        try:
            if replace_registered:
                unregister_factor(spec.id)
            register_factor(spec)
            _refresh_registered_dependents(data_dir, factor_id)
        except Exception:
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                _write_bytes_atomically(target, previous)
            replace_dynamic_factors(previous_registry)
            raise
        return get_factor(factor_id) or spec


def _direct_factor_references(spec: FactorSpec) -> set[str]:
    if spec.kind == "composite":
        return {factor_id for factor_id, _weight in spec.components}
    if spec.kind != "custom":
        return set()
    compiled = compile_formula(spec.formula_text)
    if not compiled.ok:
        first = compiled.errors[0]
        raise ValueError(f"公式无效 [{first.code}]: {first.message}")
    return set(compiled.referenced_factors)


def _refresh_registered_dependents(data_dir: Path, factor_id: str) -> None:
    """按依赖拓扑重建所有传递上游 FactorSpec。"""
    definitions = {
        str(item.get("id")): item
        for item in load_all(data_dir)
        if isinstance(item.get("id"), str)
    }
    affected = {factor_id}
    refreshed: set[str] = set()
    while True:
        changed = False
        for current in dynamic_factor_specs():
            if current.id in affected or current.id in refreshed:
                continue
            if not (_direct_factor_references(current) & affected):
                continue
            definition = definitions.get(current.id)
            if definition is None:
                raise ValueError(f"依赖因子定义缺失: {current.id}")
            updated = to_spec(definition)
            unregister_factor(current.id)
            register_factor(updated)
            affected.add(current.id)
            refreshed.add(current.id)
            changed = True
        if not changed:
            return


def _write_bytes_atomically(target: Path, payload: bytes) -> None:
    """回滚辅助: 以同目录临时文件原子恢复原始字节。"""
    fd, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".rollback",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
    finally:
        if fd >= 0:
            os.close(fd)
        temporary_path.unlink(missing_ok=True)


def load_into_registry(data_dir: Path) -> list[str]:
    """启动期把存储中的因子注册进注册表; 单个失败只跳过并告警。

    多轮加载: composite 成员可能引用尚未加载的 custom/其他 composite (文件按
    字母序加载, cf_* 先于 uf_*), 失败的 composite 延后重试, 覆盖链式引用;
    重试用尽仍失败的只告警不阻塞启动。
    """
    loaded: list[str] = []
    pending = list(load_all(data_dir))
    while pending:
        deferred: list[dict] = []
        loaded_this_round = 0
        for definition in pending:
            try:
                register_definition(definition)
                loaded.append(str(definition["id"]))
                loaded_this_round += 1
            except ValueError:
                # custom 与 composite 都可引用尚未按文件序加载的动态因子。
                # 统一延后；若一轮毫无进展，下方再逐项记录真实错误。
                deferred.append(definition)
            except Exception as exc:
                logger.warning("custom factor 注册失败 %s: %s", definition.get("id"), exc)
        if not deferred:
            break
        if loaded_this_round == 0:
            for definition in deferred:
                try:
                    register_definition(definition)
                except Exception as exc:
                    logger.warning(
                        "custom factor 注册失败 %s: %s",
                        definition.get("id"),
                        exc,
                    )
            break
        pending = deferred
    return loaded
