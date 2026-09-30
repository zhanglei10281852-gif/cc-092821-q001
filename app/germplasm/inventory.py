from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, ValidationError
from app.core.security import request_fingerprint
from app.germplasm.repository import GermplasmRepository, record


class InventoryService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)

    def create_location(self, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO storage_locations(location_code,facility,room,rack,shelf,capacity_grams,temperature_c,"
                "humidity_percent,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    data["location_code"], data["facility"], data["room"], data["rack"], data["shelf"],
                    data["capacity_grams"], data["temperature_c"], data["humidity_percent"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("库位编码已经存在") from exc
        return self.repository.location_detail(int(cursor.lastrowid))

    def change_location_status(self, location_id: int, status: str, expected_version: int) -> dict[str, Any]:
        if status not in {"active", "maintenance", "closed"}:
            raise ValidationError("库位状态无效")
        before = self.repository.require_location(location_id)
        if int(before["version"]) != expected_version:
            raise ConflictError("库位版本冲突", context={"current_version": before["version"]})
        if status == "closed" and self.repository.location_usage(location_id) > 0:
            raise ConflictError("库位中仍有种子容器，不能关闭")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE storage_locations SET status=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (status, timestamp, location_id, expected_version),
        )
        return self.repository.location_detail(location_id)

    def create_lot(self, data: dict[str, Any]) -> dict[str, Any]:
        accession = self.repository.require_accession(int(data["accession_id"]))
        if accession["status"] not in {"accepted", "restricted", "quarantine"}:
            raise ConflictError("资源尚未进入可接收状态，不能建立种子批次")
        parent = None
        if data.get("parent_lot_id"):
            parent = self.repository.require_lot(int(data["parent_lot_id"]))
            if int(parent["accession_id"]) != int(data["accession_id"]):
                raise ValidationError("子批次必须与父批次属于同一资源")
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO seed_lots(lot_no,accession_id,parent_lot_id,harvest_year,initial_weight_grams,"
                "available_weight_grams,moisture_percent,treatment,sealed_on,status,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)",
                (
                    data["lot_no"], data["accession_id"], data.get("parent_lot_id"), data["harvest_year"],
                    data["initial_weight_grams"], data["initial_weight_grams"], data.get("moisture_percent"),
                    data.get("treatment", ""), data.get("sealed_on"), data["created_by"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("种子批次编号已经存在") from exc
        lot_id = int(cursor.lastrowid)
        if parent:
            self.connection.execute(
                "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
                "VALUES(?,'盘点调整',0,?,?,?,?)",
                (lot_id, f"lineage-{lot_id}", data["created_by"], f"由父批次 {parent['lot_no']} 建立", timestamp),
            )
        return self.repository.lot_detail(lot_id)

    def place_lot(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.movement_by_key(data["idempotency_key"])
        if previous:
            placement = self.repository.require_placement(int(previous["placement_id"]))
            return {"placement": placement, "replayed": True}
        if self.repository.move_request_by_key(data["idempotency_key"]):
            raise ConflictError("同一幂等键已经用于其他库存业务")
        lot = self.repository.require_lot(int(data["lot_id"]))
        location = self.repository.require_location(int(data["location_id"]))
        if lot["status"] in {"depleted", "disposed"}:
            raise ConflictError("批次已经耗尽或报废")
        if location["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        active_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(weight_grams),0) FROM lot_placements WHERE lot_id=? AND removed_at IS NULL",
            (lot["id"],),
        ).fetchone()[0])
        if active_weight + float(data["weight_grams"]) > float(lot["available_weight_grams"]) + 1e-9:
            raise ValidationError("摆放重量超过批次可用重量")
        used = self.repository.location_usage(int(location["id"]))
        if used + float(data["weight_grams"]) > float(location["capacity_grams"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": location["capacity_grams"] - used})
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO lot_placements(lot_id,location_id,weight_grams,container_code,placed_at) VALUES(?,?,?,?,?)",
                (lot["id"], location["id"], data["weight_grams"], data["container_code"], timestamp),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("容器编码与入库时间冲突") from exc
        placement_id = int(cursor.lastrowid)
        self.connection.execute(
            "INSERT INTO lot_movements(lot_id,placement_id,movement_type,quantity_grams,to_location_id,idempotency_key,"
            "actor,reason,created_at) VALUES(?,?,'入库',?,?,?,?,?,?)",
            (lot["id"], placement_id, data["weight_grams"], location["id"], data["idempotency_key"], data["actor"], "首次入库", timestamp),
        )
        self.connection.execute(
            "UPDATE seed_lots SET status='stored',version=version+1,updated_at=? WHERE id=?",
            (timestamp, lot["id"]),
        )
        return {"placement": self.repository.require_placement(placement_id), "replayed": False}

    def move_placement(self, placement_id: int, data: dict[str, Any]) -> dict[str, Any]:
        key = data["idempotency_key"]
        target_location_id = int(data["target_location_id"])
        reason = data["reason"]
        fingerprint = request_fingerprint({
            "placement_id": placement_id,
            "target_location_id": target_location_id,
            "reason": reason,
        })

        previous = self.repository.move_request_by_key(key)
        if previous is not None:
            if previous["request_hash"] != fingerprint:
                raise ConflictError(
                    "同一幂等键已经用于其他移库请求，目标库位或原因不一致",
                    context={
                        "placement_id": previous["placement_id"],
                        "target_location_id": previous["to_location_id"],
                        "reason": previous["reason"],
                    },
                )
            return {
                "placement": self.repository.require_placement(int(previous["result_placement_id"])),
                "replayed": True,
                "moved": previous["outcome"] == "moved",
            }

        # 兼容改造前仅登记在 lot_movements 的移库流水：直接重放原结果；
        # 若该键已被其他库存业务占用，则明确报冲突。
        legacy = self.repository.movement_by_key(key)
        if legacy is not None:
            if legacy["movement_type"] != "移库":
                raise ConflictError("同一幂等键已经用于其他库存业务")
            return {
                "placement": self.repository.require_placement(int(legacy["placement_id"])),
                "replayed": True,
                "moved": True,
            }

        placement = self.repository.require_placement(placement_id)
        if placement["removed_at"]:
            raise ConflictError("容器已经移出原库位")
        if int(placement["version"]) != int(data["expected_version"]):
            raise ConflictError("容器摆放版本冲突", context={"current_version": placement["version"]})

        timestamp = to_storage(self.clock.now())
        # lot_placements 对 (container_code, placed_at) 有唯一约束，移库新记录的
        # 入库时间必须严格晚于原摆放记录，避免同一秒内移动造成键冲突。
        if timestamp <= placement["placed_at"]:
            timestamp = to_storage(from_storage(placement["placed_at"]) + timedelta(seconds=1))

        # 扫描枪重复扫到原库位：没有实际位移，不新增摆放/流水、不推进版本，
        # 仅以幂等键登记一次“无位移”结果，后续重放稳定返回同一条记录。
        if int(placement["location_id"]) == target_location_id:
            try:
                self.connection.execute(
                    "INSERT INTO movement_requests(idempotency_key,placement_id,from_location_id,to_location_id,reason,"
                    "actor,outcome,result_placement_id,result_movement_id,request_hash,created_at) "
                    "VALUES(?,?,?,?,?,?, 'no_move', ?,NULL,?,?)",
                    (
                        key, placement["id"], placement["location_id"], placement["location_id"], reason,
                        data["actor"], placement["id"], fingerprint, timestamp,
                    ),
                )
            except sqlite3.IntegrityError:
                # 延迟事务并发下另一个携带相同幂等键的请求可能抢先提交。
                # 这里主动失败、由外层事务回滚；调用方用新事务重试即可重放原结果。
                raise ConflictError("移库请求与并发的相同幂等键冲突，请重试以获取原结果") from None
            return {"placement": self.repository.require_placement(placement_id), "replayed": False, "moved": False}

        target = self.repository.require_location(target_location_id)
        if target["status"] != "active":
            raise ConflictError("目标库位当前不可用")
        # 容器当前不在目标库位（同库位情形已在上方短路），容量按目标现有占用加容器重量计算。
        used = self.repository.location_usage(target_location_id)
        if used + float(placement["weight_grams"]) > float(target["capacity_grams"]) + 1e-9:
            raise ConflictError("目标库位容量不足", context={"available_grams": target["capacity_grams"] - used})

        # 先做带版本谓词的条件更新：并发下只有一个请求能推进源记录版本，
        # 其余请求在此以业务冲突失败，不会留下半成品的新摆放行。
        updated = self.connection.execute(
            "UPDATE lot_placements SET removed_at=?,version=version+1 WHERE id=? AND version=? AND removed_at IS NULL",
            (timestamp, placement_id, data["expected_version"]),
        )
        if updated.rowcount != 1:
            raise ConflictError("容器摆放版本冲突", context={"current_version": placement["version"]})
        cursor = self.connection.execute(
            "INSERT INTO lot_placements(lot_id,location_id,weight_grams,container_code,placed_at) VALUES(?,?,?,?,?)",
            (placement["lot_id"], target["id"], placement["weight_grams"], placement["container_code"], timestamp),
        )
        new_id = int(cursor.lastrowid)
        try:
            movement_cursor = self.connection.execute(
                "INSERT INTO lot_movements(lot_id,placement_id,movement_type,quantity_grams,from_location_id,to_location_id,"
                "idempotency_key,actor,reason,created_at) VALUES(?,?,'移库',?,?,?,?,?,?,?)",
                (
                    placement["lot_id"], new_id, placement["weight_grams"], placement["location_id"], target["id"],
                    key, data["actor"], reason, timestamp,
                ),
            )
            self.connection.execute(
                "INSERT INTO movement_requests(idempotency_key,placement_id,from_location_id,to_location_id,reason,"
                "actor,outcome,result_placement_id,result_movement_id,request_hash,created_at) "
                "VALUES(?,?,?,?,?,?,'moved',?,?,?,?)",
                (
                    key, placement_id, placement["location_id"], target_location_id, reason, data["actor"],
                    new_id, int(movement_cursor.lastrowid), fingerprint, timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            # 相同幂等键已被其他业务或并发移库占用。主动失败让外层事务整体回滚，
            # 保证不会留下重复摆放/流水；调用方以新事务重试即可命中开头的重放分支。
            raise ConflictError("移库请求与已存在的幂等键冲突，请重试以获取原结果") from None
        return {"placement": self.repository.require_placement(new_id), "replayed": False, "moved": True}

    def withdraw(self, data: dict[str, Any]) -> dict[str, Any]:
        previous = self.repository.movement_by_key(data["idempotency_key"])
        if previous:
            return {"lot": self.repository.lot_detail(int(previous["lot_id"])), "movement": previous, "replayed": True}
        if self.repository.move_request_by_key(data["idempotency_key"]):
            raise ConflictError("同一幂等键已经用于其他库存业务")
        lot = self.repository.require_lot(int(data["lot_id"]))
        holds = self.repository.active_holds(int(lot["id"]))
        if holds:
            raise ConflictError("批次存在未解除的质量或权限冻结", context={"holds": [item["id"] for item in holds]})
        quantity = float(data["quantity_grams"])
        if quantity > float(lot["available_weight_grams"]) + 1e-9:
            raise ConflictError("批次可用重量不足")
        timestamp = to_storage(self.clock.now())
        remaining = round(float(lot["available_weight_grams"]) - quantity, 6)
        status = "depleted" if remaining <= 1e-9 else lot["status"]
        self.connection.execute(
            "UPDATE seed_lots SET available_weight_grams=?,status=?,version=version+1,updated_at=? WHERE id=?",
            (remaining, status, timestamp, lot["id"]),
        )
        cursor = self.connection.execute(
            "INSERT INTO lot_movements(lot_id,movement_type,quantity_grams,idempotency_key,actor,reason,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (lot["id"], data["movement_type"], -quantity, data["idempotency_key"], data["actor"], data["reason"], timestamp),
        )
        return {
            "lot": self.repository.lot_detail(int(lot["id"])),
            "movement": record(self.connection.execute("SELECT * FROM lot_movements WHERE id=?", (cursor.lastrowid,)).fetchone()),
            "replayed": False,
        }

    def impose_hold(self, data: dict[str, Any]) -> dict[str, Any]:
        lot = self.repository.require_lot(int(data["lot_id"]))
        existing = self.connection.execute(
            "SELECT * FROM lot_holds WHERE lot_id=? AND hold_type=? AND released_at IS NULL",
            (lot["id"], data["hold_type"]),
        ).fetchone()
        if existing:
            raise ConflictError("该类型冻结已经存在")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO lot_holds(lot_id,hold_type,reason,imposed_by,imposed_at) VALUES(?,?,?,?,?)",
            (lot["id"], data["hold_type"], data["reason"], data["actor"], timestamp),
        )
        self.connection.execute(
            "UPDATE seed_lots SET status='held',version=version+1,updated_at=? WHERE id=? AND status NOT IN ('depleted','disposed')",
            (timestamp, lot["id"]),
        )
        return record(self.connection.execute("SELECT * FROM lot_holds WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def release_hold(self, hold_id: int, actor: str, reason: str) -> dict[str, Any]:
        hold = record(self.connection.execute("SELECT * FROM lot_holds WHERE id=?", (hold_id,)).fetchone())
        if not hold:
            raise ValidationError("冻结记录不存在")
        if hold["released_at"]:
            raise ConflictError("冻结记录已经解除")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE lot_holds SET released_by=?,released_at=?,release_reason=? WHERE id=? AND released_at IS NULL",
            (actor, timestamp, reason, hold_id),
        )
        remaining = self.repository.active_holds(int(hold["lot_id"]))
        if not remaining:
            self.connection.execute(
                "UPDATE seed_lots SET status=CASE WHEN available_weight_grams<=0 THEN 'depleted' ELSE 'stored' END,"
                "version=version+1,updated_at=? WHERE id=? AND status='held'",
                (timestamp, hold["lot_id"]),
            )
        return record(self.connection.execute("SELECT * FROM lot_holds WHERE id=?", (hold_id,)).fetchone()) or {}

    def reconcile(self, lot_id: int) -> dict[str, Any]:
        lot = self.repository.require_lot(lot_id)
        movement_total = float(self.connection.execute(
            "SELECT COALESCE(SUM(quantity_grams),0) FROM lot_movements WHERE lot_id=? AND movement_type IN ('取样','领用','报废','归还','盘点调整')",
            (lot_id,),
        ).fetchone()[0])
        expected_available = round(float(lot["initial_weight_grams"]) + movement_total, 6)
        placed_weight = float(self.connection.execute(
            "SELECT COALESCE(SUM(weight_grams),0) FROM lot_placements WHERE lot_id=? AND removed_at IS NULL", (lot_id,)
        ).fetchone()[0])
        return {
            "lot_id": lot_id,
            "recorded_available_grams": lot["available_weight_grams"],
            "expected_available_grams": expected_available,
            "active_placement_grams": placed_weight,
            "available_matches_ledger": abs(float(lot["available_weight_grams"]) - expected_available) < 1e-6,
            "placements_within_available": placed_weight <= float(lot["available_weight_grams"]) + 1e-6,
        }
