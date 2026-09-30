from __future__ import annotations


def test_http_intake_and_inventory_flow(client, admin):
    headers = admin["headers"]
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": "HTTP-SRC-1", "provider_name": "合作站", "country_code": "CN", "locality": "北方站",
        "restrictions": {},
    })
    assert source.status_code == 201, source.text
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": "HTTP-ACC-1", "scientific_name": "Triticum aestivum", "crop_name": "小麦",
        "cultivar_name": "地方材料", "source_id": source.json()["id"], "acquisition_type": "交换",
        "received_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    })
    assert accession.status_code == 201, accession.text
    accepted = client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text
    location = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": "HTTP-L1", "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": 3000, "temperature_c": 4, "humidity_percent": 35,
    })
    assert location.status_code == 201, location.text
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": "HTTP-LOT-1", "accession_id": accepted.json()["id"], "harvest_year": 2025,
        "initial_weight_grams": 800, "moisture_percent": 8, "treatment": "清选", "created_by": "登记员",
    })
    assert lot.status_code == 201, lot.text
    placed = client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot.json()["id"], "location_id": location.json()["id"], "weight_grams": 800,
        "container_code": "HTTP-BOX-1", "idempotency_key": "http-place-0001", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    detail = client.get(f"/api/germplasm/lots/{lot.json()['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "stored"
    assert detail.json()["placements"][0]["container_code"] == "HTTP-BOX-1"


def test_api_rejects_unauthenticated_business_request(client):
    response = client.get("/api/germplasm/dashboard")
    assert response.status_code == 401


def test_api_move_same_location_is_stable_idempotent_noop(client, admin):
    headers = admin["headers"]
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": "HTTP-SRC-2", "provider_name": "合作站", "country_code": "CN", "locality": "北方站",
        "restrictions": {},
    })
    assert source.status_code == 201, source.text
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": "HTTP-ACC-2", "scientific_name": "Triticum aestivum", "crop_name": "小麦",
        "source_id": source.json()["id"], "acquisition_type": "交换", "received_on": "2026-09-20",
        "passport": {}, "created_by": "登记员",
    })
    assert accession.status_code == 201, accession.text
    accepted = client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text

    def make_location(code: str, capacity: float) -> int:
        response = client.post("/api/germplasm/locations", headers=headers, json={
            "location_code": code, "facility": "中期库", "room": "一室", "rack": "A", "shelf": "1",
            "capacity_grams": capacity, "temperature_c": 4, "humidity_percent": 35,
        })
        assert response.status_code == 201, response.text
        return response.json()["id"]

    loc_a = make_location("HTTP-L2-A", 1000)
    loc_b = make_location("HTTP-L2-B", 1000)
    loc_small = make_location("HTTP-L2-S", 100)
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": "HTTP-LOT-2", "accession_id": accepted.json()["id"], "harvest_year": 2025,
        "initial_weight_grams": 500, "moisture_percent": 8, "treatment": "清选", "created_by": "登记员",
    })
    assert lot.status_code == 201, lot.text
    placed = client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot.json()["id"], "location_id": loc_a, "weight_grams": 500,
        "container_code": "HTTP-BOX-2", "idempotency_key": "http-place-0002", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    placement_id = placed.json()["placement"]["id"]
    move_url = f"/api/germplasm/placements/{placement_id}/move"

    # 扫描枪把原库位再次扫成目标库位：得到可解释的幂等成功，而不是容量不足
    same_payload = {
        "target_location_id": loc_a, "expected_version": 1,
        "idempotency_key": "http-move-same", "actor": "盘点员", "reason": "低温库整理",
    }
    first = client.post(move_url, headers=headers, json=same_payload)
    assert first.status_code == 200, first.text
    assert first.json()["no_op"] is True
    assert first.json()["replayed"] is False
    replay = client.post(move_url, headers=headers, json=same_payload)
    assert replay.status_code == 200, replay.text
    assert replay.json()["no_op"] is True
    assert replay.json()["replayed"] is True
    assert replay.json()["placement"]["id"] == placement_id

    # 同一幂等键携带不同目标或原因：明确冲突
    conflict = client.post(move_url, headers=headers, json={**same_payload, "target_location_id": loc_b})
    assert conflict.status_code == 409
    conflict_reason = client.post(move_url, headers=headers, json={**same_payload, "reason": "另一个原因"})
    assert conflict_reason.status_code == 409

    # 查询确认：无新增摆放/流水、版本未推进、库位占用不变
    lot_detail = client.get(f"/api/germplasm/lots/{lot.json()['id']}", headers=headers).json()
    assert len(lot_detail["placements"]) == 1
    assert lot_detail["placements"][0]["version"] == 1
    assert [m["movement_type"] for m in lot_detail["movements"]] == ["入库"]
    loc_a_detail = client.get(f"/api/germplasm/locations/{loc_a}", headers=headers).json()
    assert loc_a_detail["used_grams"] == 500
    assert len(loc_a_detail["placements"]) == 1

    # 容量不足的跨库位移动失败，不留下半完成状态
    failed = client.post(move_url, headers=headers, json={
        "target_location_id": loc_small, "expected_version": 1,
        "idempotency_key": "http-move-fail", "actor": "盘点员", "reason": "库容调整",
    })
    assert failed.status_code == 409
    lot_detail = client.get(f"/api/germplasm/lots/{lot.json()['id']}", headers=headers).json()
    assert len(lot_detail["placements"]) == 1
    assert [m["movement_type"] for m in lot_detail["movements"]] == ["入库"]
    assert client.get(f"/api/germplasm/locations/{loc_small}", headers=headers).json()["used_grams"] == 0
    assert client.get(f"/api/germplasm/locations/{loc_a}", headers=headers).json()["used_grams"] == 500

    # 真正跨库位移动：原库位清空、新库位占用、产生一条移库流水
    real_payload = {
        "target_location_id": loc_b, "expected_version": 1,
        "idempotency_key": "http-move-real", "actor": "盘点员", "reason": "库容调整",
    }
    moved = client.post(move_url, headers=headers, json=real_payload)
    assert moved.status_code == 200, moved.text
    assert moved.json()["no_op"] is False
    new_placement_id = moved.json()["placement"]["id"]
    assert new_placement_id != placement_id
    assert client.get(f"/api/germplasm/locations/{loc_a}", headers=headers).json()["used_grams"] == 0
    loc_b_detail = client.get(f"/api/germplasm/locations/{loc_b}", headers=headers).json()
    assert loc_b_detail["used_grams"] == 500
    lot_detail = client.get(f"/api/germplasm/lots/{lot.json()['id']}", headers=headers).json()
    assert len([m for m in lot_detail["movements"] if m["movement_type"] == "移库"]) == 1

    # 成功重放返回最初结果，不重复产生流水
    moved_replay = client.post(move_url, headers=headers, json=real_payload)
    assert moved_replay.status_code == 200
    assert moved_replay.json()["replayed"] is True
    assert moved_replay.json()["placement"]["id"] == new_placement_id
    lot_detail = client.get(f"/api/germplasm/lots/{lot.json()['id']}", headers=headers).json()
    assert len([m for m in lot_detail["movements"] if m["movement_type"] == "移库"]) == 1

    # 已生效移库的同键不同语义请求仍然冲突
    assert client.post(move_url, headers=headers, json={**real_payload, "reason": "别的原因"}).status_code == 409


def test_api_validation_error_has_structured_body(client, admin):
    response = client.post("/api/germplasm/locations", headers=admin["headers"], json={
        "location_code": "BAD", "facility": "库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": -1, "temperature_c": 4, "humidity_percent": 35,
    })
    assert response.status_code == 422
    assert response.json()["detail"]
