from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Iterable

from pydantic import TypeAdapter

from app.schemas import EquipmentKnowledgeDetail, EquipmentKnowledgeSummary


KNOWLEDGE_PATH = Path(__file__).parent / "data" / "equipment_knowledge.json"
KNOWLEDGE_ORDER = (
    "cold_source",
    "circulation_pump",
    "cooling_tower",
    "fan_coil_unit",
    "air_handling_unit",
    "fresh_air_unit",
    "air_source_heat_pump",
    "water_source_heat_pump",
    "ground_source_heat_pump",
    "heat_exchanger",
    "compressor",
)
KNOWLEDGE_DEVICE_TYPES = {
    "cold_source": "冷源机组",
    "circulation_pump": "循环泵",
    "cooling_tower": "冷却塔",
    "fan_coil_unit": "风机盘管",
    "air_handling_unit": "组合式空调机组",
    "fresh_air_unit": "一体式新风机组",
    "air_source_heat_pump": "空气源热泵",
    "water_source_heat_pump": "水源热泵",
    "ground_source_heat_pump": "地源热泵",
    "heat_exchanger": "换热器",
    "compressor": "压缩机",
}
CATALOG_LINKS = {
    "cold_source": "冷源机组",
    "circulation_pump": "循环泵",
    "cooling_tower": "冷却塔",
    "fan_coil_unit": "风机盘管",
    "air_handling_unit": "组合式空调机组",
    "fresh_air_unit": "一体式新风机组",
    "air_source_heat_pump": "空气源热泵",
    "water_source_heat_pump": "水源热泵",
    # 目录中该类型名为「地源热泵」（见 equipment_catalog.json），必须与之一致
    "ground_source_heat_pump": "地源热泵",
}

_DETAIL_LIST_ADAPTER = TypeAdapter(list[EquipmentKnowledgeDetail])


@lru_cache(maxsize=1)
def _validated_knowledge() -> tuple[EquipmentKnowledgeDetail, ...]:
    records = _DETAIL_LIST_ADAPTER.validate_json(
        KNOWLEDGE_PATH.read_text(encoding="utf-8"),
        strict=True,
    )
    _validate_records(records)
    return tuple(records)


def load_equipment_knowledge() -> list[EquipmentKnowledgeDetail]:
    return [record.model_copy(deep=True) for record in _validated_knowledge()]


def list_equipment_knowledge() -> list[EquipmentKnowledgeSummary]:
    return [_summary(record) for record in _validated_knowledge()]


def get_equipment_knowledge(
    knowledge_id: str,
) -> EquipmentKnowledgeDetail | None:
    for record in _validated_knowledge():
        if record.knowledge_id == knowledge_id:
            return record.model_copy(deep=True)
    return None


def _summary(record: EquipmentKnowledgeDetail) -> EquipmentKnowledgeSummary:
    return EquipmentKnowledgeSummary(
        knowledge_id=record.knowledge_id,
        device_type=record.device_type,
        name=record.name,
        overview=record.overview,
        catalog_linked=record.catalog_linked,
        catalog_device_type=record.catalog_device_type,
        applicable_systems=list(record.applicable_systems),
    )


def _validate_records(records: list[EquipmentKnowledgeDetail]) -> None:
    ids = tuple(record.knowledge_id for record in records)
    if ids != KNOWLEDGE_ORDER:
        raise ValueError(
            "Equipment knowledge records must use the required stable order."
        )
    if len(set(ids)) != len(ids):
        raise ValueError("Equipment knowledge IDs must be unique.")

    for record in records:
        expected_device_type = KNOWLEDGE_DEVICE_TYPES[record.knowledge_id]
        if record.device_type != expected_device_type:
            raise ValueError(
                f"Device type for {record.knowledge_id!r} must be "
                f"{expected_device_type!r}."
            )

        expected_catalog_type = CATALOG_LINKS.get(record.knowledge_id)
        if expected_catalog_type is None:
            if record.catalog_linked or record.catalog_device_type is not None:
                raise ValueError(
                    f"Pure knowledge record {record.knowledge_id!r} cannot link "
                    "to the equipment catalog."
                )
            if any(
                parameter.catalog_field is not None
                for parameter in record.key_parameters
            ):
                raise ValueError(
                    f"Pure knowledge record {record.knowledge_id!r} cannot "
                    "reference catalog fields."
                )
        else:
            if (
                not record.catalog_linked
                or record.catalog_device_type != expected_catalog_type
            ):
                raise ValueError(
                    f"Catalog link for {record.knowledge_id!r} is inconsistent."
                )
            if any(
                parameter.catalog_field is None
                for parameter in record.key_parameters
            ):
                raise ValueError(
                    f"Catalog-linked record {record.knowledge_id!r} must "
                    "reference catalog fields."
                )

        _require_unique(
            (component.name for component in record.components),
            f"component names for {record.knowledge_id!r}",
        )
        _require_unique(
            (parameter.key for parameter in record.key_parameters),
            f"parameter keys for {record.knowledge_id!r}",
        )
        _require_unique(
            record.applicable_systems,
            f"applicable systems for {record.knowledge_id!r}",
        )
        _require_trimmed_text(record)


def _require_unique(values: Iterable[str], label: str) -> None:
    materialized = tuple(values)
    if len(set(materialized)) != len(materialized):
        raise ValueError(f"Equipment knowledge {label} must be unique.")


def _require_trimmed_text(record: EquipmentKnowledgeDetail) -> None:
    values = [
        record.knowledge_id,
        record.device_type,
        record.name,
        record.overview,
        record.principle,
        *(component.name for component in record.components),
        *(component.function for component in record.components),
        *(parameter.key for parameter in record.key_parameters),
        *(parameter.name for parameter in record.key_parameters),
        *(parameter.description for parameter in record.key_parameters),
        *(parameter.unit for parameter in record.key_parameters if parameter.unit),
    ]
    if record.catalog_device_type is not None:
        values.append(record.catalog_device_type)
    values.extend(
        parameter.catalog_field
        for parameter in record.key_parameters
        if parameter.catalog_field is not None
    )
    if any(value != value.strip() for value in values):
        raise ValueError(
            f"Equipment knowledge record {record.knowledge_id!r} contains "
            "untrimmed text."
        )
