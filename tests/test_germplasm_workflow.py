from __future__ import annotations

from datetime import date

import pytest

from app.database import get_connection, transaction
from app.germplasm.service import GermplasmService


def create_accepted_accession(service: GermplasmService, suffix: str = "001") -> dict:
    source = service.accessions.create_source({
        "source_code": f"SRC-{suffix}", "provider_name": "省级采集队", "country_code": "CN",
        "locality": "河谷试验站", "collected_on": "2025-10-02", "permit_reference": "P-88",
        "restrictions": {},
    })
    accession = service.accessions.create_accession({
        "accession_no": f"ACC-{suffix}", "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "cultivar_name": "地方材料", "source_id": source["id"], "acquisition_type": "采集",
        "received_on": "2026-09-01", "passport": {"latitude": 30.1}, "created_by": "登记员",
    })
    return service.accessions.transition(accession["id"], {
        "target_status": "accepted", "reason": "资料与检疫证明齐全", "expected_version": 1, "actor": "审核员",
    })


def create_stored_lot(service: GermplasmService, suffix: str = "001") -> tuple[dict, dict, dict]:
    accession = create_accepted_accession(service, suffix)
    location = service.inventory.create_location({
        "location_code": f"COLD-{suffix}", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
        "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
    })
    lot = service.inventory.create_lot({
        "lot_no": f"LOT-{suffix}", "accession_id": accession["id"], "parent_lot_id": None,
        "harvest_year": 2025, "initial_weight_grams": 500, "moisture_percent": 7.5,
        "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
    })
    placed = service.inventory.place_lot({
        "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 500,
        "container_code": f"BOX-{suffix}", "idempotency_key": f"place-{suffix}-0001", "actor": "保管员",
    })
    return accession, service.repository.lot_detail(lot["id"]), placed["placement"]


def test_accession_intake_and_version_conflict(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service)
        assert accession["status"] == "accepted"
        assert [item["event_type"] for item in accession["events"]] == ["created", "status_changed"]
        try:
            service.accessions.update_accession(accession["id"], {
                "crop_name": "稻", "expected_version": 1, "actor": "登记员",
            })
        except ConflictError as exc:
            assert exc.context["current_version"] == 2
        else:
            raise AssertionError("旧版本更新应被拒绝")


def test_inventory_idempotency_and_capacity(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service)
        replay = service.inventory.place_lot({
            "lot_id": lot["id"], "location_id": placement["location_id"], "weight_grams": 500,
            "container_code": "BOX-001", "idempotency_key": "place-001-0001", "actor": "保管员",
        })
        assert replay["replayed"] is True
        too_small = service.inventory.create_location({
            "location_code": "SMALL-001", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 100, "temperature_c": -18, "humidity_percent": 30,
        })
        try:
            service.inventory.move_placement(placement["id"], {
                "target_location_id": too_small["id"], "expected_version": 1,
                "idempotency_key": "move-001-0001", "actor": "保管员", "reason": "库位整理",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("容量不足的移库应被拒绝")


def test_hold_blocks_withdrawal_until_release(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service)
        hold = service.inventory.impose_hold({
            "lot_id": lot["id"], "hold_type": "质量", "reason": "等待复核", "actor": "审核员",
        })
        try:
            service.inventory.withdraw({
                "lot_id": lot["id"], "quantity_grams": 10, "movement_type": "领用",
                "idempotency_key": "withdraw-001-a", "actor": "保管员", "reason": "试验",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("冻结批次不应允许领用")
        service.inventory.release_hold(hold["id"], "审核员", "复核通过")
        result = service.inventory.withdraw({
            "lot_id": lot["id"], "quantity_grams": 10, "movement_type": "领用",
            "idempotency_key": "withdraw-001-b", "actor": "保管员", "reason": "试验",
        })
        assert result["lot"]["available_weight_grams"] == 490


def test_viability_completion_creates_schedule(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, _ = create_stored_lot(service)
        protocol = service.viability.create_protocol({
            "protocol_code": "RICE-GER", "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
            "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽发育完整",
            "created_by": "技术负责人",
        })
        service.viability.create_policy({
            "crop_name": "水稻", "risk_level": "medium", "interval_months": 12, "warning_days": 30,
            "minimum_germination_percent": 75, "effective_from": "2026-01-01", "effective_to": None,
            "created_by": "技术负责人",
        })
        test = service.viability.schedule_test({
            "test_no": "VT-001", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "周期复检",
            "sampled_grams": 5, "scheduled_for": "2026-09-25", "requested_by": "检测员",
            "idempotency_key": "schedule-vt-001",
        })
        running = service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
        assert running["status"] == "running"
        for replicate, normal in [(1, 80), (2, 82)]:
            service.viability.add_count(test["id"], {
                "replicate_no": replicate, "seeds_tested": 100, "normal_count": normal,
                "abnormal_count": 10, "dead_count": 100 - normal - 10, "fresh_count": 0,
                "observation_day": 14, "observed_by": "检测员",
            })
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        assert completed["germination_percent"] == 81
        due = service.viability.due_schedules(date(2028, 1, 1))
        assert len(due) == 1
        assert due[0]["due_on"].startswith("2027-")


def test_environment_reading_is_idempotent_and_alerts(client):
    from datetime import UTC, datetime

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        location = service.inventory.create_location({
            "location_code": "ENV-001", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        payload = {
            "location_id": location["id"], "observed_at": datetime(2026, 9, 25, 8, 0, tzinfo=UTC),
            "temperature_c": -5, "humidity_percent": 31, "source_key": "sensor-001-0800",
        }
        first = service.quality.add_reading(payload)
        second = service.quality.add_reading(payload)
        assert first["replayed"] is False and len(first["alerts"]) == 1
        assert second["replayed"] is True


def test_move_to_same_location_is_idempotent_noop(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service, "002")
        payload = {
            "target_location_id": placement["location_id"], "expected_version": 1,
            "idempotency_key": "move-same-002-0001", "actor": "盘点员", "reason": "低温库整理",
        }
        first = service.inventory.move_placement(placement["id"], payload)
        assert first["no_op"] is True
        assert first["replayed"] is False
        assert first["placement"]["id"] == placement["id"]
        assert first["placement"]["version"] == 1
        assert first["placement"]["removed_at"] is None

        # 不新增摆放或移动流水
        detail = service.repository.lot_detail(lot["id"])
        assert len(detail["placements"]) == 1
        assert [m["movement_type"] for m in detail["movements"]] == ["入库"]

        # 两个库位（此处即原库位）占用不变
        location = service.repository.location_detail(placement["location_id"])
        assert location["used_grams"] == 500

        # 成功重放返回最初结果
        replay = service.inventory.move_placement(placement["id"], dict(payload))
        assert replay["no_op"] is True
        assert replay["replayed"] is True
        assert replay["placement"]["id"] == placement["id"]
        assert service.repository.require_placement(placement["id"])["version"] == 1
        assert len(service.repository.lot_detail(lot["id"])["movements"]) == 1

        # 同一幂等键携带不同目标或原因，必须明确报冲突
        other = service.inventory.create_location({
            "location_code": "COLD-002-B", "facility": "长期库", "room": "低温二室", "rack": "R9", "shelf": "S9",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        try:
            service.inventory.move_placement(placement["id"], {
                **payload, "target_location_id": other["id"],
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("同键不同目标必须报冲突")
        try:
            service.inventory.move_placement(placement["id"], {
                **payload, "reason": "别的整理原因",
            })
        except ConflictError:
            pass
        else:
            raise AssertionError("同键不同原因必须报冲突")
        # 冲突后状态仍然干净
        assert service.repository.require_placement(placement["id"])["version"] == 1
        assert len(service.repository.lot_detail(lot["id"])["movements"]) == 1


def test_move_to_same_location_on_full_capacity_succeeds(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession = create_accepted_accession(service, "006")
        location = service.inventory.create_location({
            "location_code": "COLD-006", "facility": "长期库", "room": "低温一室", "rack": "R1", "shelf": "S1",
            "capacity_grams": 500, "temperature_c": -18, "humidity_percent": 30,
        })
        lot = service.inventory.create_lot({
            "lot_no": "LOT-006", "accession_id": accession["id"], "parent_lot_id": None,
            "harvest_year": 2025, "initial_weight_grams": 500, "moisture_percent": 7.5,
            "treatment": "清选干燥", "sealed_on": "2026-09-02", "created_by": "登记员",
        })
        placed = service.inventory.place_lot({
            "lot_id": lot["id"], "location_id": location["id"], "weight_grams": 500,
            "container_code": "BOX-006", "idempotency_key": "place-006-0001", "actor": "保管员",
        })
        detail = service.repository.location_detail(location["id"])
        assert detail["used_grams"] == detail["capacity_grams"] == 500

        # 满库位再次扫入原库位：旧逻辑会报容量不足；现在应得到幂等空操作成功
        payload = {
            "target_location_id": location["id"], "expected_version": 1,
            "idempotency_key": "move-same-006-0001", "actor": "盘点员", "reason": "低温库整理",
        }
        result = service.inventory.move_placement(placed["placement"]["id"], payload)
        assert result["no_op"] is True
        assert result["placement"]["id"] == placed["placement"]["id"]
        assert service.repository.location_detail(location["id"])["used_grams"] == 500
        replay = service.inventory.move_placement(placed["placement"]["id"], dict(payload))
        assert replay["replayed"] is True
        assert replay["placement"]["id"] == placed["placement"]["id"]
        assert len([
            m for m in service.repository.lot_detail(lot["id"])["movements"] if m["movement_type"] == "移库"
        ]) == 0


def test_failed_move_leaves_no_half_finished_state(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service, "003")
        small = service.inventory.create_location({
            "location_code": "SMALL-003", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 100, "temperature_c": -18, "humidity_percent": 30,
        })
        payload = {
            "target_location_id": small["id"], "expected_version": 1,
            "idempotency_key": "move-003-0001", "actor": "盘点员", "reason": "库容调整",
        }
        with pytest.raises(Exception):
            service.inventory.move_placement(placement["id"], payload)

        # 源容器仍在原库位、版本不变；目标库位无新增容器；没有移库流水
        source = service.repository.require_placement(placement["id"])
        assert source["removed_at"] is None and source["version"] == 1
        assert service.repository.location_detail(small["id"])["used_grams"] == 0
        movements = service.repository.lot_detail(lot["id"])["movements"]
        assert [m["movement_type"] for m in movements] == ["入库"]

        # 失败请求未占用幂等键：换一个有容量的库位用同键重试应成功
        roomy = service.inventory.create_location({
            "location_code": "ROOMY-003", "facility": "长期库", "room": "低温三室", "rack": "R3", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        retried = service.inventory.move_placement(placement["id"], {
            **payload, "target_location_id": roomy["id"],
        })
        assert retried["replayed"] is False
        assert retried["placement"]["location_id"] == roomy["id"]


def test_real_move_then_replay_and_conflict(client):
    from app.core.errors import ConflictError

    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, lot, placement = create_stored_lot(service, "004")
        target = service.inventory.create_location({
            "location_code": "COLD-004-B", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        payload = {
            "target_location_id": target["id"], "expected_version": 1,
            "idempotency_key": "move-004-0001", "actor": "盘点员", "reason": "库容调整",
        }
        moved = service.inventory.move_placement(placement["id"], payload)
        new_placement = moved["placement"]
        assert moved["no_op"] is False
        assert new_placement["location_id"] == target["id"]
        old = service.repository.require_placement(placement["id"])
        assert old["removed_at"] is not None and old["version"] == 2

        # 源库位已清空、目标库位被占用、流水恰好一条移库
        assert service.repository.location_detail(placement["location_id"])["used_grams"] == 0
        assert service.repository.location_detail(target["id"])["used_grams"] == 500
        movements = service.repository.lot_detail(lot["id"])["movements"]
        assert [m["movement_type"] for m in movements] == ["入库", "移库"]

        # 重放返回最初结果
        replay = service.inventory.move_placement(placement["id"], dict(payload))
        assert replay["replayed"] is True
        assert replay["placement"]["id"] == new_placement["id"]
        assert len([m for m in service.repository.lot_detail(lot["id"])["movements"] if m["movement_type"] == "移库"]) == 1

        # 同键不同目标/原因明确冲突
        with pytest.raises(ConflictError):
            service.inventory.move_placement(placement["id"], {
                **payload, "target_location_id": placement["location_id"],
            })
        with pytest.raises(ConflictError):
            service.inventory.move_placement(placement["id"], {**payload, "reason": "另一个原因"})


def test_concurrent_moves_apply_at_most_once(client):
    import threading

    # 先在独立事务中准备数据并提交，避免主线程持锁阻塞工作线程
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, _, placement = create_stored_lot(service, "005")
        target = service.inventory.create_location({
            "location_code": "COLD-005-B", "facility": "长期库", "room": "低温二室", "rack": "R2", "shelf": "S1",
            "capacity_grams": 1000, "temperature_c": -18, "humidity_percent": 30,
        })
        source_id = placement["location_id"]
        target_id = target["id"]
        placement_id = placement["id"]

    payload = {
        "target_location_id": target_id, "expected_version": 1,
        "idempotency_key": "move-005-0001", "actor": "盘点员", "reason": "库容调整",
    }
    outcomes: list[dict] = []
    errors: list[Exception] = []

    def worker() -> None:
        try:
            with transaction(immediate=True) as connection:
                outcomes.append(GermplasmService(connection).inventory.move_placement(placement_id, dict(payload)))
        except Exception as exc:  # noqa: BLE001 - 测试需要记录线程内任意异常
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(outcomes) == 2
    assert sorted(outcome["replayed"] for outcome in outcomes) == [False, True]
    assert len({outcome["placement"]["id"] for outcome in outcomes}) == 1
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        lot_id = service.repository.require_placement(placement_id)["lot_id"]
        moves = [
            m for m in service.repository.lot_detail(lot_id)["movements"]
            if m["movement_type"] == "移库"
        ]
        assert len(moves) == 1
        assert service.repository.location_detail(source_id)["used_grams"] == 0
        assert service.repository.location_detail(target_id)["used_grams"] == 500


def test_distribution_approval_allocates_eligible_lot(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        accession, _, _ = create_stored_lot(service)
        request = service.distribution.create_request({
            "request_no": "DIST-001", "requester": "作物研究所", "purpose": "抗旱鉴定",
            "items": [{"accession_id": accession["id"], "quantity_grams": 20}],
        })
        submitted = service.distribution.submit(request["id"], 1)
        approved = service.distribution.decide(request["id"], {
            "approve": True, "expected_version": submitted["version"], "actor": "资源审核员", "reason": "材料充足",
        })
        assert approved["status"] == "approved"
        assert approved["items"][0]["allocated_lot_id"] is not None
