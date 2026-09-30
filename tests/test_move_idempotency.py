from __future__ import annotations

import threading

import pytest

from app.core.errors import ConflictError
from app.database import transaction
from app.germplasm.service import GermplasmService
from tests.test_germplasm_workflow import create_accepted_accession, create_stored_lot


MOVE_PAYLOAD = {
    "expected_version": 1,
    "actor": "保管员",
    "reason": "低温库整理",
}


def _usage(service: GermplasmService, location_id: int) -> float:
    return service.repository.location_detail(location_id)["used_grams"]


def _movement_rows(service: GermplasmService, lot_id: int) -> list[dict]:
    return service.repository.lot_detail(lot_id)["movements"]


def test_same_location_scan_on_full_shelf_succeeds_and_retries(client):
    # 复现盘点事故：容器已放满库位（占用==容量），扫描枪又把原库位扫成目标，
    # 旧逻辑按“新增占用”计算，对满库位报容量不足；现在应稳定幂等成功。
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service, suffix="100")
        shelf = service.inventory.create_location({
            "location_code": "COLD-100", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S9",
            "capacity_grams": 500, "temperature_c": -18, "humidity_percent": 30,
        })
        lot = service.inventory.create_lot({
            "lot_no": "LOT-100", "accession_id": accession["id"], "parent_lot_id": None,
            "harvest_year": 2025, "initial_weight_grams": 500, "moisture_percent": 7.5,
            "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
        })
        placed = service.inventory.place_lot({
            "lot_id": lot["id"], "location_id": shelf["id"], "weight_grams": 500,
            "container_code": "BOX-100", "idempotency_key": "place-100-0001", "actor": "保管员",
        })["placement"]
        assert _usage(service, shelf["id"]) == 500

        result = service.inventory.move_placement(placed["id"], {
            "target_location_id": shelf["id"], "expected_version": 1,
            "idempotency_key": "move-fullshelf-0001", "actor": "保管员", "reason": "扫描重复确认",
        })
        assert result["moved"] is False
        # 事故中“随后用同一业务键重试”的场景：返回同样的结果，不制造任何歧义数据
        retry = service.inventory.move_placement(placed["id"], {
            "target_location_id": shelf["id"], "expected_version": 1,
            "idempotency_key": "move-fullshelf-0001", "actor": "保管员", "reason": "扫描重复确认",
        })
        assert retry["replayed"] is True and retry["moved"] is False
        assert retry["placement"]["id"] == placed["id"]
        assert _usage(service, shelf["id"]) == 500
        assert service.repository.require_placement(placed["id"])["version"] == 1
        assert [m["movement_type"] for m in _movement_rows(service, lot["id"])] == ["入库"]


def test_same_location_scan_is_idempotent_noop(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service)
        location_id = placement["location_id"]

        result = service.inventory.move_placement(placement["id"], {
            **MOVE_PAYLOAD, "target_location_id": location_id, "idempotency_key": "move-same-0001",
        })
        assert result["moved"] is False
        assert result["replayed"] is False

        current = service.repository.require_placement(placement["id"])
        assert current["version"] == 1
        assert current["removed_at"] is None
        assert current["location_id"] == location_id
        assert _usage(service, location_id) == 500
        # 没有新增摆放记录，也没有新增移库流水
        placements = service.repository.lot_detail(lot["id"])["placements"]
        assert len(placements) == 1
        assert [m["movement_type"] for m in _movement_rows(service, lot["id"])] == ["入库"]

        replay = service.inventory.move_placement(placement["id"], {
            **MOVE_PAYLOAD, "target_location_id": location_id, "idempotency_key": "move-same-0001",
        })
        assert replay["moved"] is False
        assert replay["replayed"] is True
        assert replay["placement"]["id"] == placement["id"]
        # 重放仍然不产生任何副作用
        assert service.repository.require_placement(placement["id"])["version"] == 1
        assert len(service.repository.lot_detail(lot["id"])["placements"]) == 1
        assert [m["movement_type"] for m in _movement_rows(service, lot["id"])] == ["入库"]


def test_same_key_different_target_or_reason_conflicts_without_side_effects(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service)
        source_id = placement["location_id"]
        other = service.inventory.create_location({
            "location_code": "COLD-002", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })

        service.inventory.move_placement(placement["id"], {
            **MOVE_PAYLOAD, "target_location_id": source_id, "idempotency_key": "move-key-0001",
        })

        with pytest.raises(ConflictError):
            service.inventory.move_placement(placement["id"], {
                **MOVE_PAYLOAD, "target_location_id": other["id"], "idempotency_key": "move-key-0001",
            })
        with pytest.raises(ConflictError):
            service.inventory.move_placement(placement["id"], {
                **MOVE_PAYLOAD, "reason": "改成别的原因",
                "target_location_id": source_id, "idempotency_key": "move-key-0001",
            })

        # 冲突请求没有半完成状态：容器仍在原库位、版本不变、无新流水
        current = service.repository.require_placement(placement["id"])
        assert current["location_id"] == source_id and current["version"] == 1
        assert current["removed_at"] is None
        assert _usage(service, source_id) == 500 and _usage(service, other["id"]) == 0
        assert len(service.repository.lot_detail(lot["id"])["placements"]) == 1
        assert [m["movement_type"] for m in _movement_rows(service, lot["id"])] == ["入库"]


def test_real_cross_location_move_atomic_and_replayable(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service)
        source_id = placement["location_id"]
        target = service.inventory.create_location({
            "location_code": "COLD-002", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })

        result = service.inventory.move_placement(placement["id"], {
            **MOVE_PAYLOAD, "target_location_id": target["id"], "idempotency_key": "move-real-0001",
        })
        assert result["moved"] is True and result["replayed"] is False
        new_placement_id = result["placement"]["id"]
        assert new_placement_id != placement["id"]

        old = service.repository.require_placement(placement["id"])
        new = service.repository.require_placement(new_placement_id)
        assert old["removed_at"] is not None and old["version"] == 2
        assert new["removed_at"] is None and new["location_id"] == target["id"]
        assert _usage(service, source_id) == 0 and _usage(service, target["id"]) == 500

        movements = _movement_rows(service, lot["id"])
        assert [m["movement_type"] for m in movements] == ["入库", "移库"]
        move_row = movements[-1]
        assert move_row["from_location_id"] == source_id
        assert move_row["to_location_id"] == target["id"]

        replay = service.inventory.move_placement(placement["id"], {
            **MOVE_PAYLOAD, "target_location_id": target["id"], "idempotency_key": "move-real-0001",
        })
        assert replay["replayed"] is True and replay["moved"] is True
        assert replay["placement"]["id"] == new_placement_id
        # 重放不产生第二条移库流水，版本不再推进
        assert [m["movement_type"] for m in _movement_rows(service, lot["id"])] == ["入库", "移库"]
        assert service.repository.require_placement(placement["id"])["version"] == 2


def test_capacity_failure_leaves_no_partial_state(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service)
        source_id = placement["location_id"]
        small = service.inventory.create_location({
            "location_code": "SMALL-002", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 100, "temperature_c": -18, "humidity_percent": 30,
        })

        with pytest.raises(ConflictError):
            service.inventory.move_placement(placement["id"], {
                **MOVE_PAYLOAD, "target_location_id": small["id"], "idempotency_key": "move-full-0001",
            })

        current = service.repository.require_placement(placement["id"])
        assert current["location_id"] == source_id and current["version"] == 1
        assert current["removed_at"] is None
        assert _usage(service, source_id) == 500 and _usage(service, small["id"]) == 0
        assert len(service.repository.lot_detail(lot["id"])["placements"]) == 1
        assert [m["movement_type"] for m in _movement_rows(service, lot["id"])] == ["入库"]


def test_stale_version_rejected(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, _, placement = create_stored_lot(service)
        target = service.inventory.create_location({
            "location_code": "COLD-003", "facility": "长期库", "room": "低温三室", "rack": "R3", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        with pytest.raises(ConflictError):
            service.inventory.move_placement(placement["id"], {
                **MOVE_PAYLOAD, "target_location_id": target["id"],
                "expected_version": 99, "idempotency_key": "move-stale-0001",
            })


def _move_in_worker(placement_id: int, payload: dict, box: dict) -> None:
    try:
        with transaction(immediate=True) as connection:
            result = GermplasmService(connection).inventory.move_placement(placement_id, payload)
            box["result"] = result
    except Exception as exc:  # noqa: BLE001 - 测试需要记录工作线程里的任意失败
        box["error"] = exc


def test_concurrent_different_keys_apply_at_most_once(client):
    # 在主线程事务之外做并发，每个工作线程使用各自的线程本地连接
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service)
        source_id = placement["location_id"]
        target = service.inventory.create_location({
            "location_code": "COLD-C1", "facility": "长期库", "room": "低温三室", "rack": "R3", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        target_id = target["id"]
        placement_id = placement["id"]
        lot_id = lot["id"]

    boxes: list[dict] = []
    threads = []
    for index in range(5):
        box: dict = {}
        boxes.append(box)
        payload = {
            **MOVE_PAYLOAD, "target_location_id": target_id,
            "idempotency_key": f"move-conc-{index:04d}",
        }
        thread = threading.Thread(target=_move_in_worker, args=(placement_id, payload, box))
        threads.append(thread)

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    successes = [box for box in boxes if "result" in box]
    failures = [box for box in boxes if "error" in box]
    assert len(successes) == 1
    assert len(failures) == 4
    assert all(isinstance(box["error"], ConflictError) for box in failures)

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        assert _usage(service, source_id) == 0
        assert _usage(service, target_id) == 500
        placements = service.repository.lot_detail(lot_id)["placements"]
        assert sum(1 for p in placements if p["removed_at"] is None) == 1
        assert sum(1 for p in placements if p["removed_at"] is not None) == 1
        assert [m["movement_type"] for m in _movement_rows(service, lot_id)].count("移库") == 1


def test_concurrent_same_key_noop_applies_at_most_once(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service, suffix="009")
        location_id = placement["location_id"]
        placement_id = placement["id"]
        lot_id = lot["id"]

    boxes: list[dict] = []
    threads = []
    for _ in range(5):
        box: dict = {}
        boxes.append(box)
        payload = {
            **MOVE_PAYLOAD, "target_location_id": location_id,
            "idempotency_key": "move-conc-same-0001",
        }
        thread = threading.Thread(target=_move_in_worker, args=(placement_id, payload, box))
        threads.append(thread)

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert all("result" in box for box in boxes)
    assert sum(1 for box in boxes if not box["result"]["replayed"]) == 1
    assert all(box["result"]["placement"]["id"] == placement_id for box in boxes)
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        assert service.repository.require_placement(placement_id)["version"] == 1
        assert len(service.repository.lot_detail(lot_id)["placements"]) == 1
        assert [m["movement_type"] for m in _movement_rows(service, lot_id)] == ["入库"]
