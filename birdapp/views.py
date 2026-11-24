from flask import (
    Blueprint,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from . import csvio
from .db import get_db
from .taxonomy import audit_trace, build_index, trace

bp = Blueprint("main", __name__)


# ---------------------------------------------------------------- 公共查询

def _latest_version(db):
    """最新版号 = 对照表最大起版+1、记录最大版号、确认最大版号、
    正式名录最大版号 四者取最大。"""
    mapping_max = db.execute("SELECT MAX(from_version) AS m FROM mapping").fetchone()["m"]
    record_max = db.execute("SELECT MAX(version) AS m FROM records").fetchone()["m"]
    confirmed_max = db.execute(
        "SELECT MAX(confirmed_version) AS m FROM records"
    ).fetchone()["m"]
    checklist_max = db.execute(
        "SELECT MAX(version) AS m FROM checklists"
    ).fetchone()["m"]
    candidates = [v for v in (record_max, confirmed_max, checklist_max) if v is not None]
    if mapping_max is not None:
        candidates.append(mapping_max + 1)
    return max(candidates) if candidates else 1


def _latest_from_snapshot(mapping_rows, records, checklist_rows):
    """与 _latest_version 同口径，但全部基于一次快照内的数据计算。"""
    mapping_max = max((r["from_version"] for r in mapping_rows), default=None)
    record_max = max((r["version"] for r in records), default=None)
    confirmed_max = max(
        (r["confirmed_version"] for r in records if r["confirmed_version"] is not None),
        default=None,
    )
    checklist_max = max((r["version"] for r in checklist_rows), default=None)
    candidates = [v for v in (record_max, confirmed_max, checklist_max) if v is not None]
    if mapping_max is not None:
        candidates.append(mapping_max + 1)
    return max(candidates) if candidates else 1


def _mapping_rows(db):
    return db.execute(
        "SELECT from_version, old_name, next_name FROM mapping "
        "ORDER BY from_version, old_name, next_name"
    ).fetchall()


def _mapping_index(db):
    return build_index(
        (r["from_version"], r["old_name"], r["next_name"]) for r in _mapping_rows(db)
    )


def _checklist_map(rows):
    """把 (version, sci_name) 行整理成 {版号: set(正式学名)}。"""
    out = {}
    for row in rows:
        out.setdefault(row["version"], set()).add(row["sci_name"])
    return out


def _load_snapshot(db):
    """在一个只读事务内一次性取出对照、正式名录和全部记录的同一已提交快照。

    汇总数字与待核清单必须来自同一状态，因此所有读取都在该事务内完成，
    计算结束后回滚释放快照（只读，不写任何数据）。
    """
    db.execute("BEGIN")
    try:
        mapping_rows = db.execute(
            "SELECT from_version, old_name, next_name FROM mapping"
        ).fetchall()
        checklist_rows = db.execute(
            "SELECT version, sci_name FROM checklists"
        ).fetchall()
        records = db.execute(
            "SELECT * FROM records ORDER BY year, record_id"
        ).fetchall()
    finally:
        db.execute("ROLLBACK")  # 只读事务，回滚即释放快照
    idx = build_index(
        (r["from_version"], r["old_name"], r["next_name"]) for r in mapping_rows
    )
    checklists = _checklist_map(checklist_rows)
    latest = _latest_from_snapshot(mapping_rows, records, checklist_rows)
    return {
        "idx": idx,
        "checklists": checklists,
        "records": records,
        "latest": latest,
    }


def _compute_pending(snapshot):
    """从每条记录当前的换算起点逐版核对正式名录，返回待核记录列表。

    - 缺正式名单的版不做收录核对，沿用原换算规则；
    - 任一路径经过未收录学名（即使后来并回有效学名）即列入待核，
      每条记录只列一次，缺名条目逐条给出 (版号, 缺名)。
    """
    idx, checklists, latest = (
        snapshot["idx"],
        snapshot["checklists"],
        snapshot["latest"],
    )
    pending = []
    for rec in snapshot["records"]:
        start_name, start_version = _record_start(rec)
        reachable, missing = audit_trace(
            {start_name}, start_version, latest, idx, checklists
        )
        if missing:
            pending.append(
                {
                    "record_id": rec["record_id"],
                    "year": rec["year"],
                    "sci_name": rec["sci_name"],
                    "version": rec["version"],
                    "count": rec["count"],
                    "confirmed_name": rec["confirmed_name"],
                    "confirmed_version": rec["confirmed_version"],
                    "start_name": start_name,
                    "start_version": start_version,
                    "missing": missing,
                    "reachable": sorted(reachable),
                }
            )
    return pending


def _latest_names(db, latest):
    """最新版全部有效学名：把出现过的每个（学名, 版号）都追到最新版取并集。"""
    idx = _mapping_index(db)
    pool = set()
    for row in db.execute(
        """
        SELECT sci_name AS n, version AS v FROM records
        UNION
        SELECT confirmed_name, confirmed_version FROM records
        WHERE confirmed_name IS NOT NULL
        UNION
        SELECT old_name, from_version FROM mapping
        UNION
        SELECT next_name, from_version + 1 FROM mapping
        """
    ):
        pool.add((row["n"], row["v"]))
    names = set()
    for name, version in pool:
        if version <= latest:
            names |= trace({name}, version, latest, idx)
    return sorted(names)


def _record_start(rec):
    """记录换算起点：已确认的从确认版换算，否则从原名原版。"""
    if rec["confirmed_name"]:
        return rec["confirmed_name"], rec["confirmed_version"]
    return rec["sci_name"], rec["version"]


def _run_aggregation(db, group):
    """按年汇总一组最新版物种的最少/最多只数，并给出同状态下的待核清单。

    单次汇总在一个只读事务内一次性取对照表、正式名录与全部记录的同一
    已提交快照，保证数字和待核清单不混用新旧数据。待核记录（任一路径经过
    未收录学名）整条暂不计入汇总，原明细保留。
    """
    snapshot = _load_snapshot(db)
    idx, checklists, latest, records = (
        snapshot["idx"],
        snapshot["checklists"],
        snapshot["latest"],
        snapshot["records"],
    )
    years = {}
    diff_records = []
    pending = []
    for rec in records:
        start_name, start_version = _record_start(rec)
        reachable, missing = audit_trace(
            {start_name}, start_version, latest, idx, checklists
        )
        if missing:
            # 任一路径经过未收录学名：整条记录暂不计入汇总
            pending.append(
                {
                    "record_id": rec["record_id"],
                    "year": rec["year"],
                    "sci_name": rec["sci_name"],
                    "version": rec["version"],
                    "count": rec["count"],
                    "confirmed_name": rec["confirmed_name"],
                    "confirmed_version": rec["confirmed_version"],
                    "start_name": start_name,
                    "start_version": start_version,
                    "missing": missing,
                    "reachable": sorted(reachable),
                }
            )
            continue
        if not (reachable & group):
            continue  # 与本组无关
        # 每条记录只计一次：即使它沿多条路径/多个学名可达本组，也不重复计入
        year_stat = years.setdefault(rec["year"], {"min": 0, "max": 0})
        year_stat["max"] += rec["count"]
        if reachable <= group:
            year_stat["min"] += rec["count"]  # 无论分类如何取舍都落在本组
        else:
            diff_records.append(
                {
                    "record_id": rec["record_id"],
                    "year": rec["year"],
                    "sci_name": rec["sci_name"],
                    "version": rec["version"],
                    "count": rec["count"],
                    "confirmed_name": rec["confirmed_name"],
                    "confirmed_version": rec["confirmed_version"],
                    "reachable": sorted(reachable),
                }
            )
    year_rows = [
        {
            "year": year,
            "min": stat["min"],
            "max": stat["max"],
            "diff_records": [
                d["record_id"] for d in diff_records if d["year"] == year
            ],
        }
        for year, stat in sorted(years.items())
    ]
    return {
        "latest_version": latest,
        "group": sorted(group),
        "years": year_rows,
        "diff_records": diff_records,
        "pending_records": pending,
        "checklist_versions": sorted(checklists),
    }


# ---------------------------------------------------------------- 页面

@bp.get("/")
def index():
    db = get_db()
    stats = {
        "records": db.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"],
        "confirmed": db.execute(
            "SELECT COUNT(*) AS c FROM records WHERE confirmed_name IS NOT NULL"
        ).fetchone()["c"],
        "mapping": db.execute("SELECT COUNT(*) AS c FROM mapping").fetchone()["c"],
        "checklists": db.execute(
            "SELECT COUNT(DISTINCT version) AS c FROM checklists"
        ).fetchone()["c"],
        "latest": _latest_version(db),
    }
    # 待核数按当前生效数据重判（名单/对照/明细/复核任一改动都会反映在这里）
    stats["pending"] = len(_compute_pending(_load_snapshot(db)))
    return render_template("index.html", stats=stats)


@bp.get("/aggregate")
def aggregate_form():
    db = get_db()
    latest = _latest_version(db)
    names = _latest_names(db, latest)
    return render_template(
        "aggregate.html", latest=latest, names=names, result=None
    )


@bp.post("/aggregate")
def aggregate_run():
    db = get_db()
    group = set(request.form.getlist("species"))
    latest = _latest_version(db)
    if not group:
        flash("请至少勾选一个最新版物种", "error")
        return redirect(url_for("main.aggregate_form"))
    result = _run_aggregation(db, group)
    if request.form.get("format") == "json":
        return jsonify(result)
    names = _latest_names(db, latest)
    return render_template(
        "aggregate.html", latest=latest, names=names, result=result
    )


@bp.get("/records")
def records_page():
    db = get_db()
    snapshot = _load_snapshot(db)
    idx, checklists, latest = (
        snapshot["idx"],
        snapshot["checklists"],
        snapshot["latest"],
    )
    missing_by_id = {}
    rows = []
    for rec in snapshot["records"]:
        start_name, start_version = _record_start(rec)
        reachable, missing = audit_trace(
            {start_name}, start_version, latest, idx, checklists
        )
        if missing:
            missing_by_id[rec["record_id"]] = missing
        rows.append({"rec": rec, "reachable": sorted(reachable)})
    all_names = [
        r["n"]
        for r in db.execute(
            "SELECT sci_name AS n FROM records UNION SELECT old_name FROM mapping "
            "UNION SELECT next_name FROM mapping ORDER BY n"
        )
    ]
    return render_template(
        "records.html",
        rows=rows,
        latest=latest,
        all_names=all_names,
        missing_by_id=missing_by_id,
    )


@bp.get("/review")
def review_page():
    """待核记录：任一路径经过未收录学名的记录（与汇总同一口径重判）。"""
    db = get_db()
    snapshot = _load_snapshot(db)
    pending = _compute_pending(snapshot)
    return render_template(
        "review.html",
        pending=pending,
        latest=snapshot["latest"],
        checklist_versions=sorted(snapshot["checklists"]),
    )


@bp.get("/mapping")
def mapping_page():
    db = get_db()
    return render_template(
        "mapping.html", rows=_mapping_rows(db), latest=_latest_version(db)
    )


@bp.get("/checklists")
def checklists_page():
    db = get_db()
    latest = _latest_version(db)
    versions = [
        {"version": r["version"], "count": r["c"]}
        for r in db.execute(
            "SELECT version, COUNT(*) AS c FROM checklists "
            "GROUP BY version ORDER BY version"
        )
    ]
    # 每个版号取前若干学名做页面预览
    preview = {}
    for v in versions:
        preview[v["version"]] = [
            r["sci_name"]
            for r in db.execute(
                "SELECT sci_name FROM checklists WHERE version = ? "
                "ORDER BY sci_name LIMIT 10",
                (v["version"],),
            )
        ]
    return render_template(
        "checklists.html",
        versions=versions,
        preview=preview,
        latest=latest,
    )


@bp.get("/api/status")
def api_status():
    db = get_db()
    snapshot = _load_snapshot(db)
    return jsonify(
        {
            "records": db.execute("SELECT COUNT(*) AS c FROM records").fetchone()["c"],
            "confirmed": db.execute(
                "SELECT COUNT(*) AS c FROM records WHERE confirmed_name IS NOT NULL"
            ).fetchone()["c"],
            "mapping_entries": db.execute("SELECT COUNT(*) AS c FROM mapping").fetchone()[
                "c"
            ],
            "checklist_versions": len(snapshot["checklists"]),
            "pending_records": len(_compute_pending(snapshot)),
            "latest_version": _latest_version(db),
        }
    )


# ---------------------------------------------------------------- 上传

@bp.post("/upload/records")
def upload_records():
    file = request.files.get("file")
    if file is None or file.filename == "":
        flash("请选择普查记录 CSV 文件", "error")
        return redirect(url_for("main.index"))
    rows, errors = csvio.parse_records(file)
    if errors:
        for err in errors[:20]:
            flash(err, "error")
        flash("普查记录上传被拒绝，未写入任何数据", "error")
        return redirect(url_for("main.index"))

    db = get_db()
    inserted = updated = confirmations_cleared = 0
    db.execute("BEGIN")
    try:
        for row in rows:
            cur = db.execute(
                "SELECT version, sci_name, confirmed_name FROM records "
                "WHERE record_id = ?",
                (row["record_id"],),
            ).fetchone()
            if cur is None:
                db.execute(
                    "INSERT INTO records (record_id, year, version, sci_name, count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        row["record_id"],
                        row["year"],
                        row["version"],
                        row["sci_name"],
                        row["count"],
                    ),
                )
                inserted += 1
            elif (
                cur["version"] == row["version"] and cur["sci_name"] == row["sci_name"]
            ):
                db.execute(
                    "UPDATE records SET year = ?, count = ? WHERE record_id = ?",
                    (row["year"], row["count"], row["record_id"]),
                )
                updated += 1
            else:
                # 原名/原版变了，原确认不再有效，一并清除
                if cur["confirmed_name"]:
                    confirmations_cleared += 1
                db.execute(
                    "UPDATE records SET year = ?, version = ?, sci_name = ?, "
                    "count = ?, confirmed_name = NULL, confirmed_version = NULL "
                    "WHERE record_id = ?",
                    (
                        row["year"],
                        row["version"],
                        row["sci_name"],
                        row["count"],
                        row["record_id"],
                    ),
                )
                updated += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash(
        "普查记录已导入：新增 %d 条，更新 %d 条，因原名/原版变更清除确认 %d 条"
        % (inserted, updated, confirmations_cleared),
        "ok",
    )
    return redirect(url_for("main.index"))


@bp.post("/upload/mapping")
def upload_mapping():
    file = request.files.get("file")
    if file is None or file.filename == "":
        flash("请选择名录对照 CSV 文件", "error")
        return redirect(url_for("main.mapping_page"))
    entries, errors = csvio.parse_mapping(file)
    if errors:
        for err in errors[:20]:
            flash(err, "error")
        flash("名录对照上传被拒绝，旧表保持不变", "error")
        return redirect(url_for("main.mapping_page"))

    db = get_db()
    new_idx = build_index(entries)
    # 整份更换时只扫描已复核（已确认）的记录，不扫描未复核的明细
    confirmed = db.execute(
        "SELECT * FROM records WHERE confirmed_name IS NOT NULL ORDER BY record_id"
    ).fetchall()
    conflicts = []
    for rec in confirmed:
        reachable = trace(
            {rec["sci_name"]}, rec["version"], rec["confirmed_version"], new_idx
        )
        if rec["confirmed_name"] not in reachable:
            conflicts.append(rec)
    if conflicts:
        ids = "、".join(rec["record_id"] for rec in conflicts)
        flash(
            "更换被拒绝：按新对照，以下 %d 条已确认记录无法从原名追到其确认的"
            "版号和学名，已保留旧表。记录号：%s" % (len(conflicts), ids),
            "error",
        )
        return redirect(url_for("main.mapping_page"))

    db.execute("BEGIN")
    try:
        db.execute("DELETE FROM mapping")
        db.executemany(
            "INSERT INTO mapping (from_version, old_name, next_name) "
            "VALUES (?, ?, ?)",
            entries,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    flash(
        "名录对照已整份更换：%d 条（已核验 %d 条已确认记录均可从原名追溯）"
        % (len(entries), len(confirmed)),
        "ok",
    )
    return redirect(url_for("main.mapping_page"))


@bp.post("/upload/checklist")
def upload_checklist():
    """整份替换某一版的正式名录（单列学名 CSV）。

    换名单时不扫描普查明细、不校验记录可达性：文件有误或保存失败时
    保留该版原名单。是否有待核记录留给下次汇总/待核页按生效数据重判。
    """
    version_raw = (request.form.get("version") or "").strip()
    try:
        version = int(version_raw)
    except ValueError:
        version = None
    if version is None or version < 1:
        flash("版号应为正整数，已保留原名单", "error")
        return redirect(url_for("main.checklists_page"))

    file = request.files.get("file")
    if file is None or file.filename == "":
        flash("请选择正式名录 CSV 文件", "error")
        return redirect(url_for("main.checklists_page"))
    names, errors = csvio.parse_checklist(file)
    if errors:
        for err in errors[:20]:
            flash(err, "error")
        flash("正式名录上传被拒绝，v%d 原名单保持不变" % version, "error")
        return redirect(url_for("main.checklists_page"))

    db = get_db()
    old_count = db.execute(
        "SELECT COUNT(*) AS c FROM checklists WHERE version = ?", (version,)
    ).fetchone()["c"]
    db.execute("BEGIN")
    try:
        db.execute("DELETE FROM checklists WHERE version = ?", (version,))
        db.executemany(
            "INSERT INTO checklists (version, sci_name) VALUES (?, ?)",
            [(version, name) for name in names],
        )
        db.commit()
    except Exception:
        db.rollback()  # 保存失败：删除已回滚，原名单保留
        flash("正式名录保存失败，v%d 原名单保持不变" % version, "error")
        return redirect(url_for("main.checklists_page"))
    flash(
        "v%d 正式名录已整份替换：%d 个学名（原名单 %d 个；本次未扫描普查明细）"
        % (version, len(names), old_count),
        "ok",
    )
    return redirect(url_for("main.checklists_page"))


# ---------------------------------------------------------------- 复核确认

@bp.post("/records/<record_id>/confirm")
def confirm(record_id):
    db = get_db()
    rec = db.execute(
        "SELECT * FROM records WHERE record_id = ?", (record_id,)
    ).fetchone()
    if rec is None:
        abort(404)

    if request.form.get("action") == "clear":
        db.execute(
            "UPDATE records SET confirmed_name = NULL, confirmed_version = NULL "
            "WHERE record_id = ?",
            (record_id,),
        )
        db.commit()
        flash("%s：已清除确认" % record_id, "ok")
        return redirect(url_for("main.records_page"))

    name = (request.form.get("confirmed_name") or "").strip()
    version_raw = (request.form.get("confirmed_version") or "").strip()
    try:
        version = int(version_raw)
    except ValueError:
        version = None
    latest = _latest_version(db)

    if not name or version is None:
        flash("%s：请填写确认学名和整数版号" % record_id, "error")
        return redirect(url_for("main.records_page"))
    if version < rec["version"] or version > latest:
        flash(
            "%s：确认版号须在原版 v%d 与最新版 v%d 之间"
            % (record_id, rec["version"], latest),
            "error",
        )
        return redirect(url_for("main.records_page"))

    idx = _mapping_index(db)
    reachable = trace({rec["sci_name"]}, rec["version"], version, idx)
    if name not in reachable:
        flash(
            "%s：按当前对照，无法从原名 %s（v%d）追到 %s（v%d）"
            % (record_id, rec["sci_name"], rec["version"], name, version),
            "error",
        )
        return redirect(url_for("main.records_page"))

    db.execute(
        "UPDATE records SET confirmed_name = ?, confirmed_version = ? "
        "WHERE record_id = ?",
        (name, version, record_id),
    )
    db.commit()
    flash(
        "%s：已确认为 %s（v%d），后续汇总从确认版换算" % (record_id, name, version),
        "ok",
    )
    return redirect(url_for("main.records_page"))
