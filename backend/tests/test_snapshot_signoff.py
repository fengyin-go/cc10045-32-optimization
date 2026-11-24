"""快照对账签认稳定性验收测试（直接运行：.venv/bin/python tests/test_snapshot_signoff.py）。

覆盖场景：
  原快照 100 同时与经办版 90（甲，差额 10）、主管版 80（乙，差额 20）对比；
  乙的签认在报告写最终输出前中断 → 第二班恢复交接；同一笔签认请求补交只出一份结论；
  撤回未签认对比不影响已签认差值；重新试算后按编号仍能分别复核 10 和 20；
  老快照只有一份对比摘要时保留原记录和能核实的版次。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

# 隔离数据库目录，必须在导入 app 模块前设置
os.environ["CC10045_DB_DIR"] = tempfile.mkdtemp(prefix="snap_signoff_test_")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import db  # noqa: E402
from app.services import service_for  # noqa: E402

svc = service_for("report_snapshot")

PASSED = 0


def check(label: str, condition: bool) -> None:
    global PASSED
    assert condition, f"FAILED: {label}"
    PASSED += 1
    print(f"  ok - {label}")


def make_snapshot(snapshot_id: int, name: str, value: float) -> None:
    db.execute(
        "INSERT INTO snapshots(id, snapshot_name, report_id, snapshot_content, status) "
        "VALUES (?,?,?,?,?)",
        [snapshot_id, name, 1,
         json.dumps({"report_name": "对账报表",
                     "sections": [{"metric_name": "对账金额", "current_value": value}]},
                    ensure_ascii=False),
         "已保存"],
    )


def main() -> None:
    make_snapshot(100, "原快照", 100)
    make_snapshot(90, "经办版", 90)
    make_snapshot(80, "主管版", 80)

    print("[1] 甲核对经办版 90：试算并签认差额 10")
    c1 = svc.compute_comparison(100, 90, scope="经办版")
    check("甲组试算成功", c1["ok"] and c1["status"] == "待签认")
    check("甲组差额为 10", c1["total_diff"] == 10)
    r1 = svc.submit_signoff(c1["comparison_id"], "REQ-A-001", "甲")
    check("甲签认出具结论", r1["ok"] and not r1["duplicated"])
    check("甲结论差额 10", r1["report"]["conclusion"]["total_diff"] == 10)

    print("[2] 乙核对主管版 80：签认已持久化，报告写出前中断")
    c2 = svc.compute_comparison(100, 80, scope="主管版")
    check("乙组差额为 20", c2["ok"] and c2["total_diff"] == 20)
    check("两组凭据各自独立成行", c2["comparison_id"] != c1["comparison_id"])

    original_issue = svc._issue_report
    svc._issue_report = lambda signoff_id: (_ for _ in ()).throw(RuntimeError("模拟中断"))
    try:
        svc.submit_signoff(c2["comparison_id"], "REQ-B-001", "乙")
        check("中断场景不应正常返回", False)
    except RuntimeError:
        pass
    finally:
        svc._issue_report = original_issue

    s2 = svc.signoff_by_request("REQ-B-001")
    check("乙签认结果已独立持久保存", s2 is not None and s2["signed_diff"] == 20)
    check("乙报告状态为待生成", s2["report_status"] == "待生成")
    check("乙尚无正式结论", svc.reconciliation_report("REQ-B-001") is None)

    print("[3] 第二班重新打开原报告：能看到乙的待交接凭据，甲的结论仍是 10")
    review = svc.snapshot_review(100)
    groups = {g["comparison_id"]: g for g in review["comparisons"]}
    check("乙组显示已签认待生成",
          groups[c2["comparison_id"]]["handoff_status"] == "已签认待生成")
    check("甲组显示已出具结论",
          groups[c1["comparison_id"]]["handoff_status"] == "已出具结论")
    pending = svc.pending_handoffs()
    check("待交接清单包含乙的请求",
          any(p["request_id"] == "REQ-B-001" for p in pending))
    r1_refresh = svc.reconciliation_report("REQ-A-001")
    check("乙待核期间刷新甲的报告仍是 10",
          r1_refresh is not None and r1_refresh["conclusion"]["total_diff"] == 10)

    print("[4] 乙按同一笔签认请求补交：只交付一份差额 20 的结论")
    r2 = svc.submit_signoff(c2["comparison_id"], "REQ-B-001", "乙")
    check("补交命中原记录", r2["ok"] and r2["duplicated"])
    check("补交结论差额 20", r2["report"]["conclusion"]["total_diff"] == 20)
    check("补交使用的是乙自己的签认记录",
          r2["report"]["signoff_id"] == s2["id"]
          and r2["report"]["comparison_id"] == c2["comparison_id"])
    check("REQ-B-001 只有一份正式结论",
          db.count("reconciliation_reports", "request_id=?", ["REQ-B-001"]) == 1)

    r2_dup = svc.submit_signoff(c2["comparison_id"], "REQ-B-001", "乙")
    check("再次重复送达返回同一份结论", r2_dup["report"]["id"] == r2["report"]["id"])
    check("重复送达后仍只有一份结论",
          db.count("reconciliation_reports", "request_id=?", ["REQ-B-001"]) == 1)

    evidence = r2["report"]["snapshot_evidence"]
    check("报告保留本次实际使用的两份快照",
          evidence["base_snapshot"]["id"] == 100 and evidence["compare_snapshot"]["id"] == 80)
    check("报告随附签认记录", evidence["signoff_record"]["request_id"] == "REQ-B-001")

    print("[5] 重新试算 100 对 80：新增版本，按编号复核两份签认仍是 10 和 20")
    c3 = svc.compute_comparison(100, 80, scope="主管版")
    check("重算生成第 2 版凭据", c3["ok"] and c3["comparison_version"] == 2)
    s1 = svc.get_signoff(r1["signoff_id"])
    s2b = svc.get_signoff(r2["signoff_id"])
    check("甲的签认仍可复核 10", s1 is not None and s1["signed_diff"] == 10)
    check("乙的签认仍可复核 20", s2b is not None and s2b["signed_diff"] == 20)

    print("[6] 撤回尚未签认的另一组对比：不更新已签认差值")
    w = svc.withdraw_comparison(c3["comparison_id"])
    check("撤回未签认对比成功", w["ok"] and w["status"] == "已撤回")
    check("撤回后甲的签认差值仍为 10", svc.get_signoff(r1["signoff_id"])["signed_diff"] == 10)
    check("撤回后乙的签认差值仍为 20", svc.get_signoff(r2["signoff_id"])["signed_diff"] == 20)
    check("甲的结论报告未被改动",
          svc.reconciliation_report("REQ-A-001")["conclusion"]["total_diff"] == 10)
    w_signed = svc.withdraw_comparison(c1["comparison_id"])
    check("已签认的对比不能撤回", not w_signed["ok"])
    w_again = svc.withdraw_comparison(c3["comparison_id"])
    check("重复撤回被拒绝", not w_again["ok"])

    print("[7] 老快照兼容：只有一份对比摘要时保留原记录和能核实的版次")
    legacy_diff = {"snapshot_a": 7, "snapshot_b": 3, "total_metrics": 2,
                   "diffs_found": 1, "details": [{"metric": "旧指标", "diff_pct": 6.5}]}
    db.execute(
        "INSERT INTO snapshots(id, snapshot_name, report_id, snapshot_content, comparison_diff, status) "
        "VALUES (?,?,?,?,?,?)",
        [7, "老快照", 1, json.dumps({"sections": []}), json.dumps(legacy_diff), "有差异"],
    )
    review_legacy = svc.snapshot_review(7)
    check("老快照按 legacy 来源返回", review_legacy["source"] == "legacy")
    check("老快照原摘要保留",
          review_legacy["legacy_summary"]["snapshot_b"] == 3)
    check("老快照能核实的版次保留",
          review_legacy["verifiable_versions"]["snapshot_a"] == 7
          and review_legacy["verifiable_versions"]["snapshot_b"] == 3)
    row7 = db.fetchone("SELECT comparison_diff FROM snapshots WHERE id=7")
    check("老快照原记录未被改写", json.loads(row7["comparison_diff"]) == legacy_diff)

    print(f"\n全部通过：{PASSED} 项检查")


if __name__ == "__main__":
    main()
