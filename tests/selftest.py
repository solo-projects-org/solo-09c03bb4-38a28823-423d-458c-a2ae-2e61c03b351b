#!/usr/bin/env python3
"""关键流程自测：上传、汇总口径、拆种/并种去重、复核确认、整份换对照。

运行：python3 tests/selftest.py
退出码 0 = 全部通过。
"""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from birdapp import create_app  # noqa: E402

MAPPING = """起版,旧学名,下一版学名
1,Larus argentatus,Larus argentatus
1,Larus argentatus,Larus michahellis
2,Larus michahellis,Larus argentatus
1,Netta rufina,Netta rufina
1,Netta rufina,Netta erythrophthalma
"""

CENSUS = """记录号,年份,名录版,学名,只数
R1,2020,1,Netta rufina,10
R2,2020,2,Netta rufina,5
R3,2021,1,Larus argentatus,7
R4,2021,2,Larus michahellis,3
R5,2021,3,Larus argentatus,4
R6,2022,1,Anas crecca,20
R7,2020,2,Netta erythrophthalma,8
"""

# 只改只数（R2: 5->6, R5: 4->9），原名/原版不变，确认应保留
CENSUS_V2 = CENSUS.replace("R2,2020,2,Netta rufina,5", "R2,2020,2,Netta rufina,6").replace(
    "R5,2021,3,Larus argentatus,4", "R5,2021,3,Larus argentatus,9"
)

# 缺少 (1, Netta rufina, Netta rufina)：已确认的 R1 将追不到确认名
MAPPING_BAD = """起版,旧学名,下一版学名
1,Larus argentatus,Larus argentatus
1,Larus argentatus,Larus michahellis
2,Larus michahellis,Larus argentatus
1,Netta rufina,Netta erythrophthalma
"""

# 保留原有追溯链，并新增 v3->v4 并种与 Anas crecca 拆种
MAPPING_GOOD = """起版,旧学名,下一版学名
1,Larus argentatus,Larus argentatus
1,Larus argentatus,Larus michahellis
2,Larus michahellis,Larus argentatus
1,Netta rufina,Netta rufina
1,Netta rufina,Netta erythrophthalma
1,Anas crecca,Anas crecca
1,Anas crecca,Anas carolinensis
3,Netta erythrophthalma,Netta rufina
"""

failures = []


def check(label, cond, detail=""):
    print("[%s] %s%s" % ("PASS" if cond else "FAIL", label,
                         "" if cond else "  -- " + str(detail)))
    if not cond:
        failures.append(label)


def upload(client, url, text):
    return client.post(
        url,
        data={"file": (io.BytesIO(text.encode("utf-8")), "upload.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def agg(client, *species):
    data = {"species": list(species), "format": "json"}
    rv = client.post("/aggregate", data=data)
    assert rv.status_code == 200, rv.status_code
    return rv.get_json()


def year(result, y):
    for row in result["years"]:
        if row["year"] == y:
            return row
    return None


def status(client):
    return client.get("/api/status").get_json()


def confirm(client, rid, name, version, action="save"):
    return client.post(
        "/records/%s/confirm" % rid,
        data={"confirmed_name": name, "confirmed_version": version, "action": action},
        follow_redirects=True,
    )


def upload_checklist(client, version, text):
    return client.post(
        "/upload/checklist",
        data={
            "version": str(version),
            "file": (io.BytesIO(text.encode("utf-8")), "checklist.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def missing_ids(pending):
    return sorted(p["record_id"] for p in pending)


def pending_of(client, *species):
    res = agg(client, *species)
    return res["pending_records"], res


def checklist_suite():
    """正式名录整份替换 + 逐版核对 + 待核记录 + 汇总排除 + 重判 的专项自测。"""
    tmp = tempfile.mkdtemp(prefix="birdapp-checklist-")
    app = create_app(
        {"DATABASE": os.path.join(tmp, "test.db"), "SECRET_KEY": "test", "TESTING": True}
    )
    client = app.test_client()

    # C1. 空库：无正式名单时不产生待核，相关页面可用
    st = status(client)
    check("待核-空库：无待核、无正式名单版号",
          st["pending_records"] == 0 and st["checklist_versions"] == 0, st)
    for path in ["/checklists", "/review"]:
        check("待核-空库 GET %s 返回 200" % path, client.get(path).status_code == 200)

    # 仅上传一份 v5 正式名单即可抬高最新版号（不依赖记录/对照）
    rv = upload_checklist(client, 5, "学名\nLarus argentatus\n")
    check("待核-上传 v5 正式名单成功", "v5 正式名录已整份替换" in rv.get_data(as_text=True))
    check("待核-最新版号随正式名单变为 v5", status(client)["latest_version"] == 5)

    # 重建一套带对照和明细的库，做完整流程
    tmp = tempfile.mkdtemp(prefix="birdapp-checklist-")
    app = create_app(
        {"DATABASE": os.path.join(tmp, "test.db"), "SECRET_KEY": "test", "TESTING": True}
    )
    client = app.test_client()
    upload(client, "/upload/mapping", MAPPING_GOOD)  # 最新版 v4
    upload(client, "/upload/records", CENSUS)

    CL_V1 = "学名\nLarus argentatus\nLarus michahellis\nNetta rufina\n" \
            "Netta erythrophthalma\nAnas crecca\n"
    CL_V2_FULL = "学名\nLarus argentatus\nLarus michahellis\nNetta rufina\n" \
                 "Netta erythrophthalma\nAnas crecca\nAnas carolinensis\n"
    CL_V2_NO_MICH = "学名\nLarus argentatus\nNetta rufina\n" \
                    "Netta erythrophthalma\nAnas crecca\nAnas carolinensis\n"
    CL_V3 = "学名\nLarus argentatus\nNetta rufina\nNetta erythrophthalma\n" \
            "Anas crecca\nAnas carolinensis\n"
    CL_V4 = "学名\nLarus argentatus\nNetta rufina\nAnas crecca\nAnas carolinensis\n"

    # C2. 坏文件/坏版号被拒绝，原名单保留
    rv = upload_checklist(client, 1, "名称\nLarus argentatus\n")
    check("待核-表头错误被拒绝", "表头应为" in rv.get_data(as_text=True))
    rv = client.post(
        "/upload/checklist",
        data={"version": "abc",
              "file": (io.BytesIO(CL_V1.encode("utf-8")), "c.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    check("待核-版号非整数被拒绝", "版号应为正整数" in rv.get_data(as_text=True))
    rv = client.post(
        "/upload/checklist",
        data={"version": "0",
              "file": (io.BytesIO(CL_V1.encode("utf-8")), "c.csv")},
        content_type="multipart/form-data", follow_redirects=True,
    )
    check("待核-版号0被拒绝", "版号应为正整数" in rv.get_data(as_text=True))
    check("待核-拒绝后无正式名单落库", status(client)["checklist_versions"] == 0)

    # C3. 只有 v1 名单：起点不在 v1 的记录不核对，暂无待核
    upload_checklist(client, 1, CL_V1)
    st = status(client)
    check("待核-仅 v1 名单时待核0、覆盖1个版号",
          st["pending_records"] == 0 and st["checklist_versions"] == 1, st)

    # C4. 补齐 v2/v3/v4 完整名单：全部学名均被收录，仍无待核
    upload_checklist(client, 2, CL_V2_FULL)
    upload_checklist(client, 3, CL_V3)
    upload_checklist(client, 4, CL_V4)
    check("待核-各版名单完整时待核0", status(client)["pending_records"] == 0)
    res = agg(client, "Larus argentatus")
    y = year(res, 2021)
    check("待核-无待核时汇总口径不变（2021=14）", y and y["min"] == 14 and y["max"] == 14, y)

    # C5. v2 名单删掉 Larus michahellis：
    # R3 拆种后一条路径在 v2 经过未收录名（v3 才并回 argentatus），仍须待核；
    # R4 起点即在 v2 的 michahellis，也待核。
    upload_checklist(client, 2, CL_V2_NO_MICH)
    pending, res = pending_of(client, "Larus argentatus")
    ids = missing_ids(pending)
    check("待核-缺名路径即使并回有效学名仍列入待核（R3、R4）", ids == ["R3", "R4"], ids)
    r3 = next(p for p in pending if p["record_id"] == "R3")
    check("待核-缺名条目给出记录号/版号/缺名",
          any(m["version"] == 2 and m["name"] == "Larus michahellis"
              for m in r3["missing"]), r3["missing"])
    check("待核-并回后可达名仍算出（Larus argentatus）",
          "Larus argentatus" in r3["reachable"], r3["reachable"])
    y = year(res, 2021)
    check("待核-待核记录整条不计入汇总（2021 只剩 R5=4）",
          y and y["min"] == 4 and y["max"] == 4, y)
    # 待核页与汇总 JSON 同口径
    review_text = client.get("/review").get_data(as_text=True)
    check("待核-/review 页面列出 R3、R4 与缺名",
          "R3" in review_text and "R4" in review_text and "Larus michahellis" in review_text)
    check("待核-汇总 HTML 页展示待核清单",
          "待核记录" in client.post(
              "/aggregate", data={"species": ["Larus argentatus"]}
          ).get_data(as_text=True))

    # C6. 恢复 v2 完整名单：按生效数据重判，待核清零、汇总恢复
    upload_checklist(client, 2, CL_V2_FULL)
    check("待核-恢复名单后重判为0待核", status(client)["pending_records"] == 0)
    res = agg(client, "Larus argentatus")
    y = year(res, 2021)
    check("待核-恢复后汇总回到 14", y and y["min"] == 14 and y["max"] == 14, y)

    # C7. 缺正式名单的版沿用原换算规则：删掉 v3 名单后，v3 不核对，仍无待核
    import sqlite3
    conn = sqlite3.connect(os.path.join(tmp, "test.db"))
    conn.execute("DELETE FROM checklists WHERE version = 3")
    conn.commit()
    conn.close()
    check("待核-v3 无名单时该版不核对、待核仍0",
          status(client)["pending_records"] == 0)

    # C8. 复核改动后按新起点重判：v1 名单去掉 Anas crecca -> R6 待核；
    # 确认 R6 为 v4 的 Anas carolinensis 后，起点后移到 v4（该名被 v4 收录）-> 解除
    upload_checklist(client, 3, CL_V3)  # 恢复 v3
    upload_checklist(
        client, 1,
        "学名\nLarus argentatus\nLarus michahellis\nNetta rufina\n"
        "Netta erythrophthalma\n",  # 不含 Anas crecca
    )
    pending, _ = pending_of(client, "Anas crecca")
    check("待核-R6 起点学名未被 v1 收录 -> 待核", missing_ids(pending) == ["R6"],
          missing_ids(pending))
    rv = confirm(client, "R6", "Anas carolinensis", "4")
    check("待核-合法确认仍被接受（确认只校验对照可达）",
          "已确认为 Anas carolinensis" in rv.get_data(as_text=True))
    check("待核-确认后从确认版重判，R6 解除待核",
          status(client)["pending_records"] == 0)
    res = agg(client, "Anas carolinensis")
    y = year(res, 2022)
    check("待核-解除后 R6 重新计入汇总（2022=20）", y and y["max"] == 20, y)

    # C9. 整份替换语义：坏文件保留原名单；好文件整份覆盖
    page = client.get("/checklists").get_data(as_text=True)
    check("待核-/checklists 展示已收录版号", "v1" in page and "v4" in page)
    before = status(client)["checklist_versions"]
    rv = upload_checklist(client, 4, "错误表头\nX\n")
    check("待核-替换坏文件被拒绝", "表头应为" in rv.get_data(as_text=True))
    check("待核-坏文件后 v4 原名单保留（版号数不变）",
          status(client)["checklist_versions"] == before)
    upload_checklist(client, 4, "学名\nOnly One Name\n")
    # v4 只剩 1 个学名：多条记录在 v4 缺名 -> 待核出现，证明替换是整份覆盖
    st = status(client)
    check("待核-v4 整份覆盖为1个学名后出现待核", st["pending_records"] > 0, st)
    upload_checklist(client, 4, CL_V4)  # 还原
    check("待核-还原 v4 后待核清零", status(client)["pending_records"] == 0)

    # C10. 汇总 JSON 的数字与待核清单同结构返回（同一已提交状态）
    res = agg(client, "Netta rufina")
    check("待核-汇总结果含 pending_records 与 checklist_versions 字段",
          "pending_records" in res and "checklist_versions" in res)
    review_ids = missing_ids(_compute_pending_http(client))
    check("待核-汇总与待核页同口径（记录号一致）",
          missing_ids(res["pending_records"]) == review_ids,
          (missing_ids(res["pending_records"]), review_ids))

    # C11. 并发：一个窗口反复汇总/看待核，另一个窗口仍能完成明细上传与复核
    import threading
    errors = []

    def reader():
        try:
            for _ in range(20):
                rv = client.post("/aggregate",
                                 data={"species": ["Netta rufina"], "format": "json"})
                assert rv.status_code == 200
                assert client.get("/review").status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(("reader", repr(exc)))

    def writer():
        try:
            extra = CENSUS + "R8,2023,1,Anas crecca,9\n"
            rv = upload(client, "/upload/records", extra)
            assert rv.status_code == 200
            rv = confirm(client, "R8", "Anas carolinensis", "4")
            assert rv.status_code == 200
            rv = confirm(client, "R8", "", "", action="clear")
            assert rv.status_code == 200
        except Exception as exc:  # noqa: BLE001
            errors.append(("writer", repr(exc)))

    t1 = threading.Thread(target=reader)
    t2 = threading.Thread(target=writer)
    t1.start(); t2.start(); t1.join(); t2.join()
    check("待核-并发读汇总与写明细/复核互不阻塞", not errors, errors)
    check("待核-并发写入的 R8 已落库", status(client)["records"] == 8)


def _compute_pending_http(client):
    """通过汇总 JSON 拿待核清单（待核与所选物种无关，选一个学名即可）。"""
    return agg(client, "Anas crecca")["pending_records"]



def main():
    tmp = tempfile.mkdtemp(prefix="birdapp-test-")
    app = create_app(
        {"DATABASE": os.path.join(tmp, "test.db"), "SECRET_KEY": "test", "TESTING": True}
    )
    client = app.test_client()

    # 1. 空库状态
    st = status(client)
    check("空库：记录0/对照0/最新版v1",
          st["records"] == 0 and st["mapping_entries"] == 0 and st["latest_version"] == 1, st)

    # 2. 上传名录对照与普查记录
    rv = upload(client, "/upload/mapping", MAPPING)
    check("上传名录对照成功", "名录对照已整份更换" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/records", CENSUS)
    check("上传普查记录成功", "新增 7 条" in rv.get_data(as_text=True))
    st = status(client)
    check("状态：7条记录/5条对照/最新版v3",
          st["records"] == 7 and st["mapping_entries"] == 5 and st["latest_version"] == 3, st)

    # 3. 最新版物种勾选列表
    page = client.get("/aggregate").get_data(as_text=True)
    check("勾选列表含最新版学名",
          all(n in page for n in ["Netta rufina", "Netta erythrophthalma",
                                  "Larus argentatus", "Anas crecca"]))
    check("勾选列表不含已被并掉的旧名 Larus michahellis",
          "Larus michahellis" not in page)

    # 4. 汇总口径：最少/最多与差额记录
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("{Netta rufina} 2020 最少5 最多15", y and y["min"] == 5 and y["max"] == 15, y)
    check("{Netta rufina} 差额记录为 R1", y and y["diff_records"] == ["R1"], y)

    # 5. 拆种后再并种：同一笔计数不沿多条路径重复计入
    res = agg(client, "Larus argentatus")
    y = year(res, 2021)
    check("{Larus argentatus} 2021 最少=最多=14（R3 沿两条路径到达但不重复计）",
          y and y["min"] == 14 and y["max"] == 14, y)

    # 6. 一条记录可达两个组内学名时仍只计一次
    res = agg(client, "Netta rufina", "Netta erythrophthalma")
    y = year(res, 2020)
    check("双种组 2020 最少=最多=23（R1 只计一次）",
          y and y["min"] == 23 and y["max"] == 23, y)

    res = agg(client, "Anas crecca")
    y = year(res, 2022)
    check("{Anas crecca} 2022 最少=最多=20（未列名沿用）",
          y and y["min"] == 20 and y["max"] == 20, y)

    # 7. 复核确认：非法确认被拒绝
    rv = confirm(client, "R2", "Anas crecca", "2")
    check("确认不可达学名被拒绝", "无法从原名" in rv.get_data(as_text=True))
    rv = confirm(client, "R3", "Larus argentatus", "9")
    check("确认版号超出最新版被拒绝", "确认版号须在原版" in rv.get_data(as_text=True))
    rv = confirm(client, "R1", "Netta rufina", "0")
    check("确认版号小于原版被拒绝", "确认版号须在原版" in rv.get_data(as_text=True))
    rv = confirm(client, "R1", "Netta rufina", "abc")
    check("确认版号非整数被拒绝", "整数版号" in rv.get_data(as_text=True))
    check("非法确认后仍无已确认记录", status(client)["confirmed"] == 0)

    # 8. 合法确认：从原名可追到，后续从确认版换算
    rv = confirm(client, "R1", "Netta rufina", "2")
    check("确认 R1=Netta rufina(v2) 成功", "已确认为 Netta rufina" in rv.get_data(as_text=True))
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("确认后 {Netta rufina} 2020 最少=最多=15（差额消除）",
          y and y["min"] == 15 and y["max"] == 15 and not y["diff_records"], y)

    # 9. 普查记录重传（同名同版只更新只数，确认保留）
    rv = upload(client, "/upload/records", CENSUS_V2)
    check("重传普查记录：更新7条", "更新 7 条" in rv.get_data(as_text=True))
    st = status(client)
    check("重传后记录仍7条、确认保留1条",
          st["records"] == 7 and st["confirmed"] == 1, st)
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("重传后 {Netta rufina} 2020 最少=最多=16", y and y["min"] == 16 and y["max"] == 16, y)
    res = agg(client, "Larus argentatus")
    y = year(res, 2021)
    check("重传后 {Larus argentatus} 2021 = 19", y and y["min"] == 19 and y["max"] == 19, y)

    # 10. 整份更换对照：已确认记录追不到 -> 列出记录号并保留旧表
    rv = upload(client, "/upload/mapping", MAPPING_BAD)
    text = rv.get_data(as_text=True)
    check("坏对照被拒绝并列出记录号 R1", "更换被拒绝" in text and "R1" in text)
    st = status(client)
    check("坏对照被拒后旧表保留（仍5条，最新版v3）",
          st["mapping_entries"] == 5 and st["latest_version"] == 3, st)
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("坏对照被拒后汇总仍按旧表（16）", y and y["min"] == 16 and y["max"] == 16, y)

    # 11. 整份更换对照：合法新表被接受（未复核明细不参与校验）
    rv = upload(client, "/upload/mapping", MAPPING_GOOD)
    check("好对照整份更换成功", "名录对照已整份更换" in rv.get_data(as_text=True))
    st = status(client)
    check("更换后8条对照、最新版v4",
          st["mapping_entries"] == 8 and st["latest_version"] == 4, st)

    # 12. 更换后单次汇总只用新对照
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("新对照下 {Netta rufina} 2020 最少=最多=24（R7 经 v3->v4 并种入组）",
          y and y["min"] == 24 and y["max"] == 24, y)
    res = agg(client, "Anas crecca")
    y = year(res, 2022)
    check("新对照下 {Anas crecca} 2022 最少0 最多20（未复核的 R6 按新表拆种）",
          y and y["min"] == 0 and y["max"] == 20 and y["diff_records"] == ["R6"], y)
    page = client.get("/aggregate").get_data(as_text=True)
    check("更换后勾选列表出现新学名 Anas carolinensis", "Anas carolinensis" in page)

    # 13. 清除确认后按原名原版重新换算
    rv = confirm(client, "R1", "", "", action="clear")
    check("清除 R1 确认成功", "已清除确认" in rv.get_data(as_text=True))
    check("清除后无已确认记录", status(client)["confirmed"] == 0)
    res = agg(client, "Netta rufina")
    y = year(res, 2020)
    check("清除确认后 {Netta rufina} 2020 仍 24（v4 下原名可达组内）",
          y and y["min"] == 24 and y["max"] == 24, y)

    # 14. CSV 校验：坏文件整份拒绝
    rv = upload(client, "/upload/records", "a,b,c\n1,2,3\n")
    check("普查记录表头错误被拒绝", "表头应为" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/records",
                "记录号,年份,名录版,学名,只数\nR8,2023,1,X,1\nR8,2023,1,X,2\n")
    check("记录号文件内重复被拒绝", "重复" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/records",
                "记录号,年份,名录版,学名,只数\nR9,2023,1,X,-3\n")
    check("只数为负被拒绝", "非负整数" in rv.get_data(as_text=True))
    rv = upload(client, "/upload/mapping", "起版,旧学名,下一版学名\nx,A,B\n")
    check("对照起版非整数被拒绝", "正整数" in rv.get_data(as_text=True))
    st = status(client)
    check("坏文件均未落库（仍7条记录/8条对照）",
          st["records"] == 7 and st["mapping_entries"] == 8, st)

    # 15. 空选择与页面可用性
    rv = client.post("/aggregate", data={}, follow_redirects=True)
    check("空选择汇总被提示", "请至少勾选" in rv.get_data(as_text=True))
    for path in ["/", "/records", "/mapping", "/aggregate"]:
        check("GET %s 返回 200" % path, client.get(path).status_code == 200)

    # 16. 正式名录 / 待核记录专项
    checklist_suite()

    print()
    if failures:
        print("失败 %d 项：%s" % (len(failures), "; ".join(failures)))
        return 1
    print("全部自测通过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
