"""数据分析管道路由：事件采集、聚合执行、指标快照、分群计算等核心操作入口。"""
from __future__ import annotations

from fastapi import APIRouter

from app.database import db
from app.pipeline import (
    check_alert, compute_aggregation, compute_cohort_comparison,
    compute_forecast, compute_funnel, compute_metric_snapshot,
    compute_retention, compute_segment, generate_report,
    ingest_events, precompute_cube, seed_real_data,
)
from app.schemas import SignoffPayload, SnapshotComparePayload
from app.services import service_for

router = APIRouter(prefix="/api/pipeline", tags=["分析管线"])


@router.post("/seed")
def api_seed_real_data() -> dict:
    """初始化全部种子数据：事件、指标、聚合窗口、分群、漏斗等。"""
    counts = seed_real_data()
    return {"ok": True, "counts": counts}


@router.post("/events/ingest")
def api_ingest_events() -> dict:
    """对所有已启用的事件定义批量摄入模拟事件。"""
    svc = service_for("event_tracker")
    result = svc.ingest_all_enabled()
    return result


@router.post("/aggregation/run-all")
def api_run_all_aggregations() -> dict:
    """对所有已启用聚合窗口执行计算。"""
    svc = service_for("realtime_agg")
    result = svc.run_all_active()
    return result


@router.post("/aggregation/{window_id}/run")
def api_run_aggregation(window_id: int) -> dict:
    """对指定聚合窗口执行单次计算。"""
    svc = service_for("realtime_agg")
    return svc.run_aggregation(window_id)


@router.post("/metrics/compute-all")
def api_compute_all_metrics() -> dict:
    """对所有指标执行快照计算。"""
    svc = service_for("metric_store")
    return svc.compute_all()


@router.post("/metrics/{metric_id}/snapshot")
def api_compute_snapshot(metric_id: int) -> dict:
    """对指定指标执行快照计算。"""
    svc = service_for("metric_store")
    return svc.compute_snapshot(metric_id)


@router.post("/segments/{segment_id}/compute")
def api_compute_segment(segment_id: int) -> dict:
    """对指定分群执行计算。"""
    svc = service_for("segment_engine")
    return svc.run_computation(segment_id)


@router.post("/funnels/{funnel_id}/compute")
def api_compute_funnel(funnel_id: int) -> dict:
    """对指定漏斗执行分析计算。"""
    svc = service_for("funnel_analyzer")
    return svc.run_computation(funnel_id)


@router.post("/retention/{task_id}/compute")
def api_compute_retention(task_id: int) -> dict:
    """对指定留存任务执行计算。"""
    svc = service_for("retention_analysis")
    return svc.run_computation(task_id)


@router.post("/forecast/{task_id}/compute")
def api_compute_forecast(task_id: int) -> dict:
    """对指定预测任务执行计算。"""
    svc = service_for("forecast_model")
    return svc.run_forecast(task_id)


@router.post("/comparison/{comparison_id}/compute")
def api_compute_comparison(comparison_id: int) -> dict:
    """对指定分群对比执行计算。"""
    svc = service_for("cohort_compare")
    return svc.run_comparison(comparison_id)


@router.post("/cube/{cube_id}/precompute")
def api_precompute_cube(cube_id: int) -> dict:
    """对指定Cube执行预计算。"""
    svc = service_for("cube_builder")
    return svc.run_precomputation(cube_id)


@router.post("/report/{report_id}/generate")
def api_generate_report(report_id: int) -> dict:
    """对指定报告模板执行生成。"""
    svc = service_for("data_report")
    return svc.generate(report_id)


@router.post("/alerts/check-all")
def api_check_all_alerts() -> dict:
    """检查所有已生效告警规则。"""
    svc = service_for("alert_policy")
    return svc.check_all()


# ---------------------------------------------------------------------------
# 对账快照对比 / 差额签认 / 结论复查
# ---------------------------------------------------------------------------

@router.post("/snapshots/{snapshot_id}/compare")
def api_compare_snapshot(snapshot_id: int, payload: SnapshotComparePayload) -> dict:
    """试算一组快照对比，保存差异凭据（同组重算只新增版本，不动已签认差值）。"""
    svc = service_for("report_snapshot")
    return svc.compute_comparison(snapshot_id, payload.compare_snapshot_id, payload.scope)


@router.post("/comparisons/{comparison_id}/signoff")
def api_signoff_comparison(comparison_id: int, payload: SignoffPayload) -> dict:
    """受理差额签认并出具对账结论；同一 request_id 重复送达只产生一份正式结论。"""
    svc = service_for("report_snapshot")
    return svc.submit_signoff(comparison_id, payload.request_id, payload.signer)


@router.post("/comparisons/{comparison_id}/withdraw")
def api_withdraw_comparison(comparison_id: int) -> dict:
    """撤回尚未签认的一组对比，不影响其他组已签认的差值。"""
    svc = service_for("report_snapshot")
    return svc.withdraw_comparison(comparison_id)


@router.post("/signoffs/{request_id}/resume")
def api_resume_signoff(request_id: str) -> dict:
    """交接班恢复：按签认请求编号补出具未生成的对账结论。"""
    svc = service_for("report_snapshot")
    return svc.resume_signoff(request_id)


@router.get("/signoffs/pending")
def api_pending_handoffs() -> dict:
    """下一班可继续处理的凭据：已签认但结论未生成的签认记录。"""
    svc = service_for("report_snapshot")
    return {"ok": True, "items": svc.pending_handoffs()}


@router.get("/signoffs/{signoff_id}")
def api_get_signoff(signoff_id: int) -> dict:
    """按编号复核签认记录及其冻结的差异凭据。"""
    svc = service_for("report_snapshot")
    signoff = svc.get_signoff(signoff_id)
    if signoff is None:
        return {"ok": False, "message": f"签认记录 {signoff_id} 不存在"}
    return {"ok": True, "signoff": signoff}


@router.get("/snapshots/{snapshot_id}/review")
def api_snapshot_review(snapshot_id: int) -> dict:
    """重新打开原快照的复查视图：逐组对比凭据 + 签认交接状态（老快照回退原摘要）。"""
    svc = service_for("report_snapshot")
    return svc.snapshot_review(snapshot_id)


@router.get("/reconciliation-reports/{request_id}")
def api_reconciliation_report(request_id: str) -> dict:
    """按签认请求编号查已出具的正式对账结论。"""
    svc = service_for("report_snapshot")
    report = svc.reconciliation_report(request_id)
    if report is None:
        return {"ok": False, "message": f"签认请求 {request_id} 尚未出具结论"}
    return {"ok": True, "report": report}


@router.get("/analytics/full-pipeline")
def api_full_pipeline() -> dict:
    """执行完整分析链路：事件采集→聚合→指标→分群→漏斗→留存→预测→报告。"""
    steps = {}

    # 1. 事件采集
    svc_events = service_for("event_tracker")
    steps["events"] = svc_events.ingest_all_enabled()

    # 2. 聚合
    svc_agg = service_for("realtime_agg")
    steps["aggregation"] = svc_agg.run_all_active()

    # 3. 指标快照
    svc_metrics = service_for("metric_store")
    steps["metrics"] = svc_metrics.compute_all()

    # 4. 分群
    segments = db.fetchall("SELECT id FROM segments LIMIT 6")
    seg_results = []
    for s in segments:
        r = service_for("segment_engine").run_computation(s["id"])
        seg_results.append(r)
    steps["segments"] = {"processed": len(segments), "results": seg_results}

    # 5. 漏斗
    funnels = db.fetchall("SELECT id FROM funnels LIMIT 3")
    funnel_results = []
    for f in funnels:
        r = service_for("funnel_analyzer").run_computation(f["id"])
        funnel_results.append(r)
    steps["funnels"] = {"processed": len(funnels), "results": funnel_results}

    # 6. 留存
    ret_tasks = db.fetchall("SELECT id FROM retention_tasks LIMIT 3")
    ret_results = []
    for t in ret_tasks:
        r = service_for("retention_analysis").run_computation(t["id"])
        ret_results.append(r)
    steps["retention"] = {"processed": len(ret_tasks), "results": ret_results}

    # 7. 预测
    forecast_tasks = db.fetchall("SELECT id FROM forecast_tasks LIMIT 2")
    fc_results = []
    for t in forecast_tasks:
        r = service_for("forecast_model").run_forecast(t["id"])
        fc_results.append(r)
    steps["forecast"] = {"processed": len(forecast_tasks), "results": fc_results}

    # 8. 报告
    reports = db.fetchall("SELECT id FROM reports LIMIT 2")
    report_results = []
    for rep in reports:
        r = service_for("data_report").generate(rep["id"])
        report_results.append(r)
    steps["reports"] = {"processed": len(reports), "results": report_results}

    return {"ok": True, "pipeline": steps}