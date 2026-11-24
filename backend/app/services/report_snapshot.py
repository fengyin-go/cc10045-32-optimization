"""报告快照领域服务：保存报告的完整数据快照并支持历史版本对比与对账签认。

真实数据流：从 report_outputs 读取已有报告内容 → 生成快照记录写入 snapshots 表
→ 同一份原快照可同时与多个经办版本试算对比，每组（原快照, 对比版本, 对比范围）
  独立保存一份差异凭据（snapshot_comparisons），供复核经办人分别签认
→ 签认结果独立持久化（signoff_records，request_id 幂等），数据报告出具对账结论时
  使用已签认的差异凭据，并把本次采用的快照对比数据与签认记录随报告一起保存
  （reconciliation_reports）
→ 同一笔签认请求重复送达只产生一份正式结论；中断后交接班可恢复"已签认待生成"的凭据。

兼容约定：老快照只有 snapshots.comparison_diff 一份对比摘要时，保留原记录，
复查视图按 legacy 来源返回原摘要和能核实的版次，不做迁移改写。
"""
from __future__ import annotations

import json
from typing import Any

from app.database import db
from app.services.base import BaseModuleService


def _parse_json(raw: Any) -> Any:
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _diff_sections(content_a: dict[str, Any], content_b: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    """逐项计算两版快照的指标差值，返回（明细, 差额合计）。"""
    details: list[dict[str, Any]] = []
    total = 0.0
    sections_a = content_a.get("sections", [])
    sections_b = content_b.get("sections", [])
    for sa, sb in zip(sections_a, sections_b):
        val_a = sa.get("current_value")
        val_b = sb.get("current_value")
        if val_a is None or val_b is None:
            continue
        try:
            fa, fb = float(val_a), float(val_b)
        except (TypeError, ValueError):
            continue
        diff_value = round(fa - fb, 4)
        diff_pct = round(diff_value / max(abs(fb), 0.0001) * 100, 2)
        details.append({
            "metric": sa.get("metric_name"),
            "value_a": fa,
            "value_b": fb,
            "diff_value": diff_value,
            "diff_pct": diff_pct,
            "has_diff": abs(diff_pct) > 1.0,
        })
        total += diff_value
    return details, round(total, 4)


def _parse_signoff(row: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(row)
    try:
        parsed["diff_snapshot"] = _parse_json(parsed.get("diff_snapshot") or {})
    except json.JSONDecodeError:
        pass
    return parsed


def _parse_report(row: dict[str, Any]) -> dict[str, Any]:
    parsed = dict(row)
    for key in ("conclusion", "snapshot_evidence"):
        try:
            parsed[key] = _parse_json(parsed.get(key) or {})
        except json.JSONDecodeError:
            pass
    return parsed


class ReportSnapshotService(BaseModuleService):
    MODULE = "report_snapshot"
    ENTITY = "快照记录"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["快照名称", "所属报告", "快照时间", "数据摘要", "差异对比"]
    REQUIRED_FIELDS = ["快照名称", "所属报告"]
    STATUS_ORDER = ["已保存", "已对比", "有差异"]
    ACTION_RULES = {"生成快照": "已保存", "对比快照": "已对比"}
    NEGATIVE_ACTIONS = []

    def create_for_report(self, report_id: int, snapshot_name: str) -> dict[str, Any]:
        """为指定报告的最新输出创建快照。"""
        latest_output = db.fetchone(
            "SELECT * FROM report_outputs WHERE report_id=? ORDER BY generated_at DESC LIMIT 1",
            [report_id],
        )
        if not latest_output:
            return {"ok": False, "message": f"报告 {report_id} 没有已生成的输出"}

        snapshot_id = db.insert("snapshots", {
            "snapshot_name": snapshot_name,
            "report_id": report_id,
            "snapshot_content": latest_output["output_content"],
            "status": "已保存",
        })
        return {
            "ok": True,
            "snapshot_id": snapshot_id,
            "snapshot_name": snapshot_name,
            "report_id": report_id,
        }

    # ------------------------------------------------------------------
    # 差异凭据：每组（原快照, 对比版本, 对比范围）独立成行，重新试算只新增版本
    # ------------------------------------------------------------------

    def compute_comparison(
        self,
        base_snapshot_id: int,
        compare_snapshot_id: int,
        scope: str = "",
    ) -> dict[str, Any]:
        """试算一组快照对比并保存差异凭据。

        同一份原快照可同时与多个经办版本对比，各自独立成行；
        重新试算同一组只递增 comparison_version 新增记录，绝不更新已签认的差值。
        """
        a = db.fetchone("SELECT * FROM snapshots WHERE id=?", [base_snapshot_id])
        b = db.fetchone("SELECT * FROM snapshots WHERE id=?", [compare_snapshot_id])
        if not a or not b:
            return {"ok": False, "message": "快照不存在"}

        try:
            content_a = _parse_json(a["snapshot_content"])
            content_b = _parse_json(b["snapshot_content"])
        except json.JSONDecodeError:
            return {"ok": False, "message": "快照内容解析失败"}

        details, total_diff = _diff_sections(content_a, content_b)
        has_diff = any(d["has_diff"] for d in details)

        diff_data = {
            "snapshot_a": base_snapshot_id,
            "snapshot_b": compare_snapshot_id,
            "scope": scope,
            "total_metrics": len(details),
            "diffs_found": sum(1 for d in details if d["has_diff"]),
            "total_diff": total_diff,
            "details": details,
        }
        # 旧版摘要字段保持原结构，仅作"最近一次试算"缓存；权威数据以差异凭据表为准
        legacy_summary = {
            "snapshot_a": base_snapshot_id,
            "snapshot_b": compare_snapshot_id,
            "total_metrics": len(details),
            "diffs_found": sum(1 for d in details if d["has_diff"]),
            "details": details,
        }

        with db.tx() as conn:
            version_row = conn.execute(
                "SELECT COALESCE(MAX(comparison_version), 0) + 1 AS v FROM snapshot_comparisons "
                "WHERE base_snapshot_id=? AND compare_snapshot_id=? AND compare_scope=?",
                [base_snapshot_id, compare_snapshot_id, scope],
            ).fetchone()
            version = int(version_row["v"])
            cursor = conn.execute(
                "INSERT INTO snapshot_comparisons("
                "base_snapshot_id, compare_snapshot_id, compare_scope, comparison_version, "
                "diff_data, total_diff, status) VALUES (?,?,?,?,?,?, '待签认')",
                [base_snapshot_id, compare_snapshot_id, scope, version,
                 json.dumps(diff_data, ensure_ascii=False), total_diff],
            )
            comparison_id = int(cursor.lastrowid or 0)
            legacy_cache = dict(legacy_summary, latest_comparison_id=comparison_id)
            conn.execute(
                "UPDATE snapshots SET status=?, comparison_diff=? WHERE id=?",
                ["有差异" if has_diff else "已对比",
                 json.dumps(legacy_cache, ensure_ascii=False), base_snapshot_id],
            )

        return {
            "ok": True,
            "comparison_id": comparison_id,
            "comparison_version": version,
            "status": "待签认",
            "has_difference": has_diff,
            "total_diff": total_diff,
            "summary": legacy_summary,
        }

    def compare_snapshots(self, snapshot_id_a: int, snapshot_id_b: int) -> dict[str, Any]:
        """对比两个快照的差异（旧接口）。

        现在每次对比都会落一份独立的差异凭据（待签认），返回结构保持兼容。
        """
        result = self.compute_comparison(snapshot_id_a, snapshot_id_b, scope="")
        if not result["ok"]:
            return result
        return {
            "ok": True,
            "has_difference": result["has_difference"],
            "summary": result["summary"],
            "comparison_id": result["comparison_id"],
            "comparison_version": result["comparison_version"],
            "status": "待签认",
        }

    def withdraw_comparison(self, comparison_id: int) -> dict[str, Any]:
        """撤回一组尚未签认的对比；只更新本组记录，已签认的差值不受影响。"""
        comp = db.fetchone("SELECT * FROM snapshot_comparisons WHERE id=?", [comparison_id])
        if not comp:
            return {"ok": False, "message": f"差异凭据 {comparison_id} 不存在"}
        if comp["status"] == "已签认":
            return {"ok": False, "message": "该组对比已签认，差值已冻结，不能撤回"}
        if comp["status"] == "已撤回":
            return {"ok": False, "message": "该组对比已撤回"}
        db.update("snapshot_comparisons", {"status": "已撤回"},
                  "id=? AND status='待签认'", [comparison_id])
        return {
            "ok": True,
            "comparison_id": comparison_id,
            "status": "已撤回",
            "message": "已撤回未签认对比，已签认差值不受影响",
        }

    # ------------------------------------------------------------------
    # 签认：独立持久化 + request_id 幂等；结论报告单独落库，支持中断恢复
    # ------------------------------------------------------------------

    def submit_signoff(
        self,
        comparison_id: int,
        request_id: str,
        signer: str = "",
    ) -> dict[str, Any]:
        """受理一笔差额签认请求并出具对账结论。

        第一阶段把签认结果独立持久化（request_id 唯一，重复送达直接命中原记录）；
        第二阶段依据冻结的差异凭据出具结论报告。若两阶段之间中断，签认已落库、
        报告待生成，交接班可用同一 request_id 恢复，且只产生一份正式结论。
        """
        request_id = (request_id or "").strip()
        if not request_id:
            return {"ok": False, "message": "缺少签认请求编号 request_id"}

        duplicated = False
        with db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM signoff_records WHERE request_id=?", [request_id],
            ).fetchone()
            if row is None:
                comp = conn.execute(
                    "SELECT * FROM snapshot_comparisons WHERE id=?", [comparison_id],
                ).fetchone()
                if comp is None:
                    return {"ok": False, "message": f"差异凭据 {comparison_id} 不存在"}
                if comp["status"] == "已撤回":
                    return {"ok": False, "message": "该组对比已撤回，不能签认"}
                if comp["status"] != "待签认":
                    return {"ok": False, "message": "该组对比已签认，不能重复签认"}
                # 签认时冻结差异凭据内容，后续试算/撤回都不再影响本次签认差值
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO signoff_records("
                    "request_id, comparison_id, signer, signed_diff, diff_snapshot, "
                    "status, report_status) VALUES (?,?,?,?,?, '已签认', '待生成')",
                    [request_id, comparison_id, signer,
                     comp["total_diff"], comp["diff_data"]],
                )
                if cursor.rowcount == 0:
                    duplicated = True  # 并发下同一请求已被其他线程受理
                else:
                    conn.execute(
                        "UPDATE snapshot_comparisons SET status='已签认', signed_at=? WHERE id=?",
                        [db.now(), comparison_id],
                    )
            else:
                duplicated = True
            row = conn.execute(
                "SELECT * FROM signoff_records WHERE request_id=?", [request_id],
            ).fetchone()

        signoff = _parse_signoff(dict(row))
        issued = self._issue_report(signoff["id"])
        if not issued["ok"]:
            return issued
        if duplicated:
            message = "同一签认请求重复送达，返回原正式结论"
        else:
            message = "签认已受理，对账结论已出具"
        return {
            "ok": True,
            "signoff_id": signoff["id"],
            "request_id": request_id,
            "comparison_id": signoff["comparison_id"],
            "duplicated": duplicated,
            "report": issued["report"],
            "message": message,
        }

    def resume_signoff(self, request_id: str) -> dict[str, Any]:
        """交接班恢复：按签认请求编号补出具未生成的对账结论（只处理原记录）。"""
        request_id = (request_id or "").strip()
        signoff = db.fetchone("SELECT * FROM signoff_records WHERE request_id=?", [request_id])
        if not signoff:
            return {"ok": False, "message": f"签认请求 {request_id} 不存在"}
        issued = self._issue_report(signoff["id"])
        if not issued["ok"]:
            return issued
        return {
            "ok": True,
            "resumed": True,
            "signoff_id": signoff["id"],
            "request_id": request_id,
            "comparison_id": signoff["comparison_id"],
            "report": issued["report"],
            "message": "已恢复待交接凭据并出具对账结论" if not issued["duplicated"]
                       else "该请求已出具结论，返回原正式结论",
        }

    def _issue_report(self, signoff_id: int) -> dict[str, Any]:
        """依据已签认的差异凭据出具对账结论（request_id 唯一，只产生一份）。

        结论使用签认时冻结的差值，并把本次采用的两份快照内容与签认记录
        一并冻结进 snapshot_evidence 随报告保存。
        """
        signoff = db.fetchone("SELECT * FROM signoff_records WHERE id=?", [signoff_id])
        if not signoff:
            return {"ok": False, "message": "签认记录不存在"}
        comp = db.fetchone(
            "SELECT * FROM snapshot_comparisons WHERE id=?", [signoff["comparison_id"]])
        if not comp:
            return {"ok": False, "message": "差异凭据不存在"}

        base = db.fetchone("SELECT * FROM snapshots WHERE id=?", [comp["base_snapshot_id"]])
        compare = db.fetchone("SELECT * FROM snapshots WHERE id=?", [comp["compare_snapshot_id"]])

        diff_data = _parse_json(signoff["diff_snapshot"] or {})
        conclusion = {
            "title": f"对账结论：快照 {comp['base_snapshot_id']} 对 {comp['compare_snapshot_id']}",
            "total_diff": signoff["signed_diff"],
            "diff_details": diff_data.get("details", []),
            "scope": comp["compare_scope"],
            "signer": signoff["signer"],
            "comparison_version": comp["comparison_version"],
        }
        evidence = {
            "base_snapshot": _snapshot_evidence(base),
            "compare_snapshot": _snapshot_evidence(compare),
            "signoff_record": {
                "id": signoff["id"],
                "request_id": signoff["request_id"],
                "signer": signoff["signer"],
                "signed_diff": signoff["signed_diff"],
                "signed_at": signoff["created_at"],
            },
        }

        with db.tx() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO reconciliation_reports("
                "request_id, signoff_id, comparison_id, base_snapshot_id, compare_snapshot_id, "
                "conclusion, snapshot_evidence, status) VALUES (?,?,?,?,?,?,?, '已出具')",
                [signoff["request_id"], signoff_id, comp["id"],
                 comp["base_snapshot_id"], comp["compare_snapshot_id"],
                 json.dumps(conclusion, ensure_ascii=False),
                 json.dumps(evidence, ensure_ascii=False)],
            )
            if cursor.rowcount == 0:
                duplicated = True
            else:
                duplicated = False
                conn.execute(
                    "UPDATE signoff_records SET status='已出具结论', report_status='已生成' WHERE id=?",
                    [signoff_id],
                )
            row = conn.execute(
                "SELECT * FROM reconciliation_reports WHERE request_id=?",
                [signoff["request_id"]],
            ).fetchone()

        return {"ok": True, "duplicated": duplicated, "report": _parse_report(dict(row))}

    # ------------------------------------------------------------------
    # 复查：按快照/签认编号复核，老快照回退到原有对比摘要
    # ------------------------------------------------------------------

    def snapshot_review(self, snapshot_id: int) -> dict[str, Any]:
        """重新打开原快照的复查视图：逐组展示对比凭据与签认交接状态。

        老快照若只有一份对比摘要（comparison_diff），保留原记录并按 legacy
        来源返回能核实的版次，不做迁移改写。
        """
        snap = db.fetchone("SELECT * FROM snapshots WHERE id=?", [snapshot_id])
        if not snap:
            return {"ok": False, "message": "快照不存在"}

        comps = db.fetchall(
            "SELECT * FROM snapshot_comparisons WHERE base_snapshot_id=? OR compare_snapshot_id=? "
            "ORDER BY id",
            [snapshot_id, snapshot_id],
        )
        groups: list[dict[str, Any]] = []
        for comp in comps:
            signoff = db.fetchone(
                "SELECT * FROM signoff_records WHERE comparison_id=? ORDER BY id DESC LIMIT 1",
                [comp["id"]],
            )
            report = db.fetchone(
                "SELECT * FROM reconciliation_reports WHERE comparison_id=? ORDER BY id DESC LIMIT 1",
                [comp["id"]],
            )
            if comp["status"] == "已撤回":
                handoff = "已撤回"
            elif signoff and signoff["report_status"] == "待生成":
                handoff = "已签认待生成"
            elif signoff:
                handoff = "已出具结论"
            else:
                handoff = "待签认"
            groups.append({
                "comparison_id": comp["id"],
                "base_snapshot_id": comp["base_snapshot_id"],
                "compare_snapshot_id": comp["compare_snapshot_id"],
                "scope": comp["compare_scope"],
                "comparison_version": comp["comparison_version"],
                "total_diff": comp["total_diff"],
                "status": comp["status"],
                "handoff_status": handoff,
                "signoff": _parse_signoff(signoff) if signoff else None,
                "report": _parse_report(report) if report else None,
            })

        result: dict[str, Any] = {
            "ok": True,
            "snapshot_id": snapshot_id,
            "source": "snapshot_comparisons",
            "comparisons": groups,
        }
        if not groups and snap.get("comparison_diff"):
            try:
                legacy = _parse_json(snap["comparison_diff"])
            except json.JSONDecodeError:
                legacy = {"raw": snap["comparison_diff"]}
            result["source"] = "legacy"
            result["legacy_summary"] = legacy
            result["verifiable_versions"] = {
                "snapshot_a": legacy.get("snapshot_a") if isinstance(legacy, dict) else None,
                "snapshot_b": legacy.get("snapshot_b") if isinstance(legacy, dict) else None,
                "snapshot_status": snap["status"],
                "snapshot_created_at": snap["created_at"],
            }
        return result

    def pending_handoffs(self) -> list[dict[str, Any]]:
        """下一班可继续处理的凭据：已签认但结论报告尚未生成的签认记录。"""
        rows = db.fetchall(
            "SELECT * FROM signoff_records WHERE report_status='待生成' ORDER BY id")
        return [_parse_signoff(r) for r in rows]

    def get_signoff(self, signoff_id: int) -> dict[str, Any] | None:
        """按编号复核签认记录（含冻结的差异凭据与已出具的结论）。"""
        row = db.fetchone("SELECT * FROM signoff_records WHERE id=?", [signoff_id])
        if not row:
            return None
        parsed = _parse_signoff(row)
        report = db.fetchone(
            "SELECT * FROM reconciliation_reports WHERE request_id=?", [row["request_id"]])
        parsed["report"] = _parse_report(report) if report else None
        return parsed

    def signoff_by_request(self, request_id: str) -> dict[str, Any] | None:
        """按签认请求编号查原记录。"""
        row = db.fetchone("SELECT * FROM signoff_records WHERE request_id=?", [request_id])
        return _parse_signoff(row) if row else None

    def reconciliation_report(self, request_id: str) -> dict[str, Any] | None:
        """按签认请求编号查已出具的正式结论。"""
        row = db.fetchone(
            "SELECT * FROM reconciliation_reports WHERE request_id=?", [request_id])
        return _parse_report(row) if row else None

    def report_snapshots(self, report_id: int) -> list[dict[str, Any]]:
        """获取报告的所有快照。"""
        return db.fetchall(
            "SELECT * FROM snapshots WHERE report_id=? ORDER BY created_at DESC",
            [report_id],
        )

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["total_snapshots"] = db.count("snapshots")
        snapshot["total_comparisons"] = db.count("snapshot_comparisons")
        snapshot["total_signoffs"] = db.count("signoff_records")
        snapshot["total_reconciliation_reports"] = db.count("reconciliation_reports")
        snapshot["pending_handoffs"] = db.count(
            "signoff_records", "report_status=?", ["待生成"])
        return snapshot


def _snapshot_evidence(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """把一份快照的内容冻结为报告证据。"""
    if not snapshot:
        return None
    try:
        content = _parse_json(snapshot["snapshot_content"])
    except json.JSONDecodeError:
        content = {"raw": snapshot["snapshot_content"]}
    return {
        "id": snapshot["id"],
        "snapshot_name": snapshot["snapshot_name"],
        "status": snapshot["status"],
        "created_at": snapshot["created_at"],
        "content": content,
    }
