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


def test_api_validation_error_has_structured_body(client, admin):
    response = client.post("/api/germplasm/locations", headers=admin["headers"], json={
        "location_code": "BAD", "facility": "库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": -1, "temperature_c": 4, "humidity_percent": 35,
    })
    assert response.status_code == 422
    assert response.json()["detail"]


def _seed_lot_in_two_locations(client, headers, suffix: str, capacity: float = 3000):
    location = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": f"API-SRC-{suffix}", "facility": "低温库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": capacity, "temperature_c": -18, "humidity_percent": 30,
    })
    assert location.status_code == 201, location.text
    target = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": f"API-DST-{suffix}", "facility": "低温库", "room": "二室", "rack": "B", "shelf": "1",
        "capacity_grams": capacity, "temperature_c": -18, "humidity_percent": 30,
    })
    assert target.status_code == 201, target.text
    source = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": f"API-S-{suffix}", "provider_name": "合作站", "country_code": "CN",
        "locality": "北方站", "restrictions": {},
    })
    assert source.status_code == 201, source.text
    accession = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": f"API-A-{suffix}", "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "source_id": source.json()["id"], "acquisition_type": "交换",
        "received_on": "2026-09-20", "passport": {}, "created_by": "登记员",
    })
    assert accession.status_code == 201, accession.text
    accepted = client.post(f"/api/germplasm/accessions/{accession.json()['id']}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text
    lot = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": f"API-L-{suffix}", "accession_id": accepted.json()["id"], "harvest_year": 2025,
        "initial_weight_grams": 800, "moisture_percent": 8, "treatment": "清选", "created_by": "登记员",
    })
    assert lot.status_code == 201, lot.text
    placed = client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot.json()["id"], "location_id": location.json()["id"], "weight_grams": 800,
        "container_code": f"API-BOX-{suffix}", "idempotency_key": f"api-place-{suffix}", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    return lot.json()["id"], placed.json()["placement"], location.json()["id"], target.json()["id"]


def test_api_same_location_move_is_idempotent_noop(client, admin):
    headers = admin["headers"]
    lot_id, placement, source_id, _ = _seed_lot_in_two_locations(client, headers, "01")
    body = {
        "target_location_id": source_id, "expected_version": 1,
        "idempotency_key": "api-move-same-01", "actor": "保管员", "reason": "扫描重复确认",
    }
    first = client.post(f"/api/germplasm/placements/{placement['id']}/move", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["moved"] is False and first.json()["replayed"] is False
    replay = client.post(f"/api/germplasm/placements/{placement['id']}/move", headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["moved"] is False and replay.json()["replayed"] is True
    assert replay.json()["placement"]["id"] == placement["id"]

    source = client.get(f"/api/germplasm/locations/{source_id}", headers=headers).json()
    lot = client.get(f"/api/germplasm/lots/{lot_id}", headers=headers).json()
    assert source["used_grams"] == 800
    assert len(source["placements"]) == 1
    assert lot["placements"][0]["version"] == 1
    assert [m["movement_type"] for m in lot["movements"]] == ["入库"]
    # 无位移请求在请求台账中可查，结果为 no_move，便于盘点人员解释重放
    requests_seen = lot["move_requests"]
    assert len(requests_seen) == 1
    assert requests_seen[0]["outcome"] == "no_move"
    assert requests_seen[0]["idempotency_key"] == "api-move-same-01"
    assert requests_seen[0]["to_location_id"] == source_id
    assert requests_seen[0]["result_movement_id"] is None


def test_api_move_conflict_and_failure_leave_no_partial_state(client, admin):
    headers = admin["headers"]
    lot_id, placement, source_id, target_id = _seed_lot_in_two_locations(client, headers, "02")
    body = {
        "target_location_id": source_id, "expected_version": 1,
        "idempotency_key": "api-move-key-02", "actor": "保管员", "reason": "扫描重复确认",
    }
    assert client.post(f"/api/germplasm/placements/{placement['id']}/move", headers=headers, json=body).status_code == 200

    conflict = client.post(f"/api/germplasm/placements/{placement['id']}/move", headers=headers, json={
        **body, "target_location_id": target_id,
    })
    assert conflict.status_code == 409

    # 容量不足的失败请求：通过 API 查询确认没有半完成状态
    too_small = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": "API-DST-TINY", "facility": "低温库", "room": "三室", "rack": "C", "shelf": "1",
        "capacity_grams": 100, "temperature_c": -18, "humidity_percent": 30,
    }).json()
    failed = client.post(f"/api/germplasm/placements/{placement['id']}/move", headers=headers, json={
        "target_location_id": too_small["id"], "expected_version": 1,
        "idempotency_key": "api-move-full-02", "actor": "保管员", "reason": "库位整理",
    })
    assert failed.status_code == 409

    source = client.get(f"/api/germplasm/locations/{source_id}", headers=headers).json()
    target = client.get(f"/api/germplasm/locations/{target_id}", headers=headers).json()
    tiny = client.get(f"/api/germplasm/locations/{too_small['id']}", headers=headers).json()
    lot = client.get(f"/api/germplasm/lots/{lot_id}", headers=headers).json()
    assert source["used_grams"] == 800 and len(source["placements"]) == 1
    assert target["used_grams"] == 0 and len(target["placements"]) == 0
    assert tiny["used_grams"] == 0 and len(tiny["placements"]) == 0
    assert len(lot["placements"]) == 1 and lot["placements"][0]["version"] == 1
    assert [m["movement_type"] for m in lot["movements"]] == ["入库"]
    # 失败请求（键冲突、容量不足）不在请求台账中留下记录
    assert [r["idempotency_key"] for r in lot["move_requests"]] == ["api-move-key-02"]


def test_api_real_move_then_replay_returns_original_result(client, admin):
    headers = admin["headers"]
    lot_id, placement, source_id, target_id = _seed_lot_in_two_locations(client, headers, "03")
    body = {
        "target_location_id": target_id, "expected_version": 1,
        "idempotency_key": "api-move-real-03", "actor": "保管员", "reason": "库位整理",
    }
    first = client.post(f"/api/germplasm/placements/{placement['id']}/move", headers=headers, json=body)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert payload["moved"] is True and payload["replayed"] is False
    new_placement_id = payload["placement"]["id"]
    assert new_placement_id != placement["id"]

    replay = client.post(f"/api/germplasm/placements/{placement['id']}/move", headers=headers, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["replayed"] is True
    assert replay.json()["placement"]["id"] == new_placement_id

    source = client.get(f"/api/germplasm/locations/{source_id}", headers=headers).json()
    target = client.get(f"/api/germplasm/locations/{target_id}", headers=headers).json()
    lot = client.get(f"/api/germplasm/lots/{lot_id}", headers=headers).json()
    assert source["used_grams"] == 0 and target["used_grams"] == 800
    assert len(target["placements"]) == 1 and target["placements"][0]["id"] == new_placement_id
    assert [m["movement_type"] for m in lot["movements"]] == ["入库", "移库"]
    assert len([m for m in lot["movements"] if m["movement_type"] == "移库"]) == 1
    requests_seen = lot["move_requests"]
    assert len(requests_seen) == 1
    assert requests_seen[0]["outcome"] == "moved"
    assert requests_seen[0]["result_movement_id"] == lot["movements"][-1]["id"]
    assert requests_seen[0]["result_placement_id"] == new_placement_id
