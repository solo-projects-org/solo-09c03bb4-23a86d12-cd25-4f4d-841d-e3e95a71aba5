import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app, db, services  # noqa: E402


def csv_bytes(month, rows, header=("月份", "摊位", "品种", "每公斤报价")):
    lines = [",".join(header)]
    for stall, item, price in rows:
        lines.append(f"{month},{stall},{item},{price}")
    return ("\n".join(lines) + "\n").encode("utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, month, rows, follow=True):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(csv_bytes(month, rows)), f"{month}.csv")},
            content_type="multipart/form-data",
            follow_redirects=follow,
        )

    def table(self):
        resp = self.client.get("/index.csv")
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True).strip().splitlines()
        out = {}
        for line in text[1:]:
            month, pairs, factor, index, status = line.split(",")
            out[month] = {
                "pairs": int(pairs) if pairs else None,
                "factor": float(factor) if factor else None,
                "index": float(index) if index else None,
                "status": status,
            }
        return out

    def conn(self):
        return db.connect(self.db_path)


class TestParsing(Base):
    def test_header_alias_and_english(self):
        raw = b"month,stall,item,price\n2025-01,A,\xe5\x9c\x9f\xe8\xb1\x86,3.5\n"
        res = services.parse_csv(raw)
        self.assertEqual(res.month, "2025-01")
        self.assertEqual(res.rows[0].price, 3.5)

    def test_duplicate_rejected(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3), ("A", "土豆", 4)])
        with self.assertRaises(services.UploadError) as ctx:
            services.parse_csv(raw)
        self.assertIn("只收一条", ctx.exception.message)

    def test_non_positive_price_rejected(self):
        for bad in ("0", "-2", "abc", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(services.UploadError):
                    services.parse_csv(csv_bytes("2025-01", [("A", "土豆", bad)]))

    def test_multiple_months_rejected(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3)]) + b"2025-02,B,\xe5\x9c\x9f\xe8\xb1\x86,3\n"
        with self.assertRaises(services.UploadError):
            services.parse_csv(raw)

    def test_bad_month_format_rejected(self):
        raw = csv_bytes("2025-13", [("A", "土豆", 3)])
        with self.assertRaises(services.UploadError):
            services.parse_csv(raw)

    def test_gbk_file_supported(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3)]).decode("utf-8").encode("gb18030")
        self.assertEqual(services.parse_csv(raw).month, "2025-01")


class TestIndexMath(Base):
    def test_base_month_is_100_and_factor_is_equal_weighted_mean(self):
        self.upload("2025-01", [("A", "土豆", 2.0), ("B", "土豆", 4.0)])
        self.upload("2025-02", [("A", "土豆", 2.2), ("B", "土豆", 4.4)])
        t = self.table()
        self.assertEqual(t["2025-01"]["index"], 100.0)
        self.assertEqual(t["2025-01"]["status"], "基月")
        self.assertEqual(t["2025-02"]["pairs"], 2)
        self.assertAlmostEqual(t["2025-02"]["factor"], 1.1, places=6)
        self.assertAlmostEqual(t["2025-02"]["index"], 110.0, places=4)

    def test_only_matching_stall_item_pairs_count(self):
        self.upload("2025-01", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        self.upload("2025-02", [("A", "土豆", 3.0), ("C", "白菜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 1)
        self.assertAlmostEqual(t["2025-02"]["factor"], 1.5, places=6)
        self.assertAlmostEqual(t["2025-02"]["index"], 150.0, places=4)

    def test_chain_compounds(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        self.upload("2025-03", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-02"]["index"], 200.0, places=4)
        self.assertAlmostEqual(t["2025-03"]["index"], 200.0, places=4)

    def test_missing_month_breaks_chain_and_backfill_restores(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        self.upload("2025-04", [("A", "土豆", 4.0)])
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "缺月")
        self.assertIsNone(t["2025-03"]["index"])
        self.assertIsNone(t["2025-04"]["index"])
        self.assertEqual(t["2025-04"]["status"], "断链")

        self.upload("2025-03", [("A", "土豆", 4.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-03"]["index"], 400.0, places=4)
        self.assertAlmostEqual(t["2025-04"]["index"], 400.0, places=4)
        self.assertEqual(t["2025-04"]["status"], "正常")

    def test_no_pairs_breaks_chain(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("Q", "黄瓜", 9.0)])
        self.upload("2025-03", [("Q", "黄瓜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 0)
        self.assertEqual(t["2025-02"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-03"]["index"])

    def test_earlier_than_base_rejected(self):
        self.upload("2025-03", [("A", "土豆", 1.0)])
        resp = self.upload("2025-01", [("A", "土豆", 1.0)])
        self.assertIn("早于基月", resp.get_data(as_text=True))
        self.assertNotIn("2025-01", self.table())

    def test_base_month_fixed_after_first_upload(self):
        self.upload("2025-03", [("A", "土豆", 1.0)])
        self.upload("2025-05", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "基月")
        self.assertEqual(t["2025-05"]["status"], "断链")

    def test_reupload_replaces_whole_month(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        self.upload("2025-02", [("A", "土豆", 3.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 1)
        self.assertAlmostEqual(t["2025-02"]["factor"], 3.0, places=6)
        conn = self.conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) c FROM prices WHERE month='2025-02'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(count, 1)


class TestIncremental(Base):
    def test_correction_only_recomputes_neighbouring_factors(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 2.0)]:
            self.upload(month, [("A", "土豆", price)])
        before = self.table()
        # 标记 2025-01 的环比不参与（基月），比较 2/3 月
        self.upload("2025-02", [("A", "土豆", 3.0)])
        after = self.table()
        self.assertAlmostEqual(after["2025-02"]["factor"], 3.0, places=6)
        self.assertAlmostEqual(after["2025-02"]["index"], 300.0, places=4)
        # 3 月环比 = 2.0 / 3.0
        self.assertAlmostEqual(after["2025-03"]["factor"], 2.0 / 3.0, places=3)
        self.assertAlmostEqual(after["2025-03"]["index"], 200.0, places=4)
        # 基月未变
        self.assertEqual(before["2025-01"]["index"], after["2025-01"]["index"])

    def test_break_and_repair_via_replace(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 2.5)]:
            self.upload(month, [("A", "土豆", price)])
        self.upload("2025-02", [("Z", "黄瓜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-02"]["index"])
        self.assertIsNone(t["2025-03"]["index"])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-03"]["index"], 250.0, places=4)

    def test_only_two_factors_recomputed_on_replace(self):
        for month, price in [
            ("2025-01", 1.0),
            ("2025-02", 2.0),
            ("2025-03", 2.5),
            ("2025-04", 2.5),
        ]:
            self.upload(month, [("A", "土豆", price)])

        conn = self.conn()
        try:
            with db.transaction(conn):
                # 篡改一个「不应被重算」的环比，重算后应原样保留
                conn.execute(
                    "UPDATE index_months SET mom_factor = 1.5 WHERE month = '2025-04'"
                )
        finally:
            conn.close()

        self.upload("2025-02", [("A", "土豆", 3.0)])
        conn = self.conn()
        try:
            rows = {
                r["month"]: r
                for r in conn.execute("SELECT * FROM index_months").fetchall()
            }
        finally:
            conn.close()
        # 2、3 月环比被重算
        self.assertAlmostEqual(rows["2025-02"]["mom_factor"], 3.0, places=6)
        self.assertAlmostEqual(rows["2025-03"]["mom_factor"], 2.5 / 3.0, places=6)
        # 4 月环比沿用，不被重算
        self.assertAlmostEqual(rows["2025-04"]["mom_factor"], 1.5, places=6)
        # 但指数全部重新串联
        self.assertAlmostEqual(rows["2025-04"]["index_value"], 300.0 * 2.5 / 3.0 * 1.5, places=3)


class TestReupload(Base):
    """重复上传：按摊位/品种增量写入，整月归新批次。"""

    def uploads(self):
        conn = self.conn()
        try:
            return {
                r["id"]: dict(r)
                for r in conn.execute("SELECT * FROM uploads ORDER BY id").fetchall()
            }
        finally:
            conn.close()

    def prices(self, month):
        conn = self.conn()
        try:
            return {
                (r["stall"], r["item"]): (r["price"], r["upload_id"])
                for r in conn.execute(
                    "SELECT * FROM prices WHERE month = ?", (month,)
                ).fetchall()
            }
        finally:
            conn.close()

    def index_rows(self):
        conn = self.conn()
        try:
            return {
                r["month"]: dict(r)
                for r in conn.execute("SELECT * FROM index_months").fetchall()
            }
        finally:
            conn.close()

    def changes(self, upload_id):
        conn = self.conn()
        try:
            return [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM price_changes WHERE upload_id = ? ORDER BY id",
                    (upload_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

    def test_identical_reupload_registers_batch_without_price_writes(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        before_prices = self.prices("2025-02")
        before_index = self.index_rows()
        before_count = len(self.uploads())

        resp = self.upload(
            "2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)]
        )
        text = resp.get_data(as_text=True)
        self.assertIn("批次 #3", text)
        self.assertIn("新增 0、改价 0、撤下 0", text)

        uploads = self.uploads()
        self.assertEqual(len(uploads), before_count + 1)
        batch = uploads[3]
        self.assertEqual(batch["row_count"], 2)
        self.assertEqual(
            (batch["added_count"], batch["changed_count"], batch["removed_count"]),
            (0, 0, 0),
        )
        self.assertEqual(batch["unchanged_count"], 2)
        # 未变行一个字段都不写：价格值相同，价格行归属仍是原批次
        self.assertEqual(self.prices("2025-02"), before_prices)
        # 整月报价归新批次记在月份层级：指数来源指向新批次
        after_index = self.index_rows()
        self.assertEqual(after_index["2025-02"]["mom_factor"],
                         before_index["2025-02"]["mom_factor"])
        self.assertEqual(after_index["2025-02"]["source_upload_id"], 3)
        self.assertEqual(after_index["2025-01"]["source_upload_id"], 1)
        self.assertEqual(self.changes(3), [])

    def test_identical_reupload_recomputes_neither_factor(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 3.0)]:
            self.upload(month, [("A", "土豆", price)])
        conn = self.conn()
        try:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE index_months SET mom_factor = 7.0"
                    " WHERE month IN ('2025-02', '2025-03')"
                )
        finally:
            conn.close()
        self.upload("2025-02", [("A", "土豆", 2.0)])
        rows = self.index_rows()
        # 无可比报价对变化 → 两段环比都沿用（哨兵保留）
        self.assertAlmostEqual(rows["2025-02"]["mom_factor"], 7.0, places=6)
        self.assertAlmostEqual(rows["2025-03"]["mom_factor"], 7.0, places=6)
        # 指数仍按沿用后的系数重新串联
        self.assertAlmostEqual(rows["2025-02"]["index_value"], 700.0, places=3)
        self.assertAlmostEqual(rows["2025-03"]["index_value"], 4900.0, places=3)

    def test_add_change_remove_counts_and_rows(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload(
            "2025-02",
            [("A", "土豆", 2.0), ("A", "白菜", 4.0), ("B", "黄瓜", 6.0)],
        )
        resp = self.upload("2025-02", [("A", "土豆", 3.0), ("C", "菠菜", 8.0)])
        text = resp.get_data(as_text=True)
        self.assertIn("新增 1、改价 1、撤下 2", text)

        prices = {k: v[0] for k, v in self.prices("2025-02").items()}
        self.assertEqual(prices, {("A", "土豆"): 3.0, ("C", "菠菜"): 8.0})

        changes = self.changes(3)
        self.assertEqual(
            [(c["stall"], c["item"], c["change_type"], c["old_price"], c["new_price"])
             for c in changes],
            [
                ("C", "菠菜", "add", None, 8.0),
                ("A", "土豆", "change", 2.0, 3.0),
                ("A", "白菜", "remove", 4.0, None),
                ("B", "黄瓜", "remove", 6.0, None),
            ],
        )
        # 新增/改价/撤下都会影响与 1 月的可比报价对
        self.assertAlmostEqual(self.index_rows()["2025-02"]["mom_factor"], 3.0, places=6)

    def test_one_sided_change_only_touches_one_factor(self):
        # C 只在 2 月出现：改它只影响 2 月环比（对 1 月），不影响 3 月环比。
        self.upload("2025-01", [("A", "土豆", 1.0), ("C", "菠菜", 2.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("C", "菠菜", 4.0)])
        self.upload("2025-03", [("A", "土豆", 3.0)])
        before = self.index_rows()
        self.assertAlmostEqual(before["2025-02"]["mom_factor"], 2.0, places=6)
        self.assertAlmostEqual(before["2025-03"]["mom_factor"], 1.5, places=6)

        conn = self.conn()
        try:
            with db.transaction(conn):
                # 若 3 月环比被误重算，这个哨兵值会消失
                conn.execute(
                    "UPDATE index_months SET mom_factor = 9.0 WHERE month = '2025-03'"
                )
        finally:
            conn.close()

        self.upload("2025-02", [("A", "土豆", 2.0), ("C", "菠菜", 8.0)])
        after = self.index_rows()
        self.assertAlmostEqual(after["2025-02"]["mom_factor"], 3.0, places=6)
        # 3 月环比沿用（哨兵保留），指数按新 2 月指数重连
        self.assertAlmostEqual(after["2025-03"]["mom_factor"], 9.0, places=6)
        self.assertAlmostEqual(after["2025-03"]["index_value"], 300.0 * 9.0, places=3)
        self.assertEqual(after["2025-03"]["pair_count"], 1)

    def test_change_unpaired_to_previous_month_still_touches_next_factor(self):
        # B 只在 2、3 月有：改 2 月的 B 不影响 2 月环比（1 月无 B），
        # 但会影响 3 月环比（3 月有 B）。
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 4.0)])
        self.upload("2025-03", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        before = self.index_rows()
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 8.0)])
        after = self.index_rows()
        # 2 月环比不变；3 月环比变为 (2/2 + 5/8)/2
        self.assertEqual(before["2025-02"]["mom_factor"], after["2025-02"]["mom_factor"])
        self.assertAlmostEqual(after["2025-03"]["mom_factor"], (1.0 + 5 / 8) / 2, places=6)

    def test_interleaved_reupload_compares_against_latest_before_this_one(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        # 模拟两次重报交错：第二次以第一次生效后的报价为基准
        self.upload("2025-02", [("A", "土豆", 3.0)])
        resp = self.upload("2025-02", [("A", "土豆", 3.0), ("B", "白菜", 5.0)])
        self.assertIn("新增 1、改价 0、撤下 0", resp.get_data(as_text=True))
        self.assertAlmostEqual(
            self.index_rows()["2025-02"]["mom_factor"], 3.0, places=6
        )

    def test_failed_reupload_keeps_previous_prices_batches_and_index(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        before_index = self.index_rows()
        before_uploads = self.uploads()
        before_prices = self.prices("2025-02")

        conn = self.conn()
        try:
            conn.execute(
                "CREATE TRIGGER boom_upload BEFORE INSERT ON uploads "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        finally:
            conn.close()

        resp = self.upload("2025-02", [("A", "土豆", 9.0), ("B", "白菜", 5.0)])
        self.assertIn("回滚", resp.get_data(as_text=True))
        self.assertEqual(self.uploads(), before_uploads)
        self.assertEqual(self.prices("2025-02"), before_prices)
        self.assertEqual(self.index_rows(), before_index)

    def test_base_month_change_recomputes_next_factor(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        self.upload("2025-03", [("A", "土豆", 2.0)])
        before = self.index_rows()
        self.assertAlmostEqual(before["2025-02"]["mom_factor"], 2.0, places=6)
        self.upload("2025-01", [("A", "土豆", 0.5)])
        after = self.index_rows()
        self.assertEqual(after["2025-01"]["index_value"], 100.0)
        self.assertAlmostEqual(after["2025-02"]["mom_factor"], 4.0, places=6)
        self.assertAlmostEqual(after["2025-03"]["mom_factor"], 1.0, places=6)
        self.assertAlmostEqual(after["2025-03"]["index_value"], 400.0, places=3)

    def test_backfill_missing_month_counts_as_new(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-03", [("A", "土豆", 3.0)])
        self.assertEqual(self.index_rows()["2025-02"]["has_upload"], 0)
        resp = self.upload("2025-02", [("Z", "黄瓜", 9.0)])
        self.assertIn("新增 1、改价 0、撤下 0", resp.get_data(as_text=True))
        rows = self.index_rows()
        self.assertEqual(rows["2025-02"]["has_upload"], 1)
        self.assertEqual(rows["2025-02"]["pair_count"], 0)
        self.assertEqual(rows["2025-03"]["pair_count"], 0)

    def test_failed_index_write_rolls_back_prices_and_batch(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        before_index = self.index_rows()
        before_uploads = self.uploads()
        before_prices = self.prices("2025-02")

        conn = self.conn()
        try:
            # 失败发生在报价已写入、指数落库时：前面所有写入都必须回退
            conn.execute(
                "CREATE TRIGGER boom_index BEFORE INSERT ON index_months "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        finally:
            conn.close()

        # 改价 + 新增，确保前面的写价格步骤已经执行
        resp = self.upload("2025-02", [("A", "土豆", 9.0), ("B", "白菜", 5.0)])
        self.assertIn("回滚", resp.get_data(as_text=True))
        self.assertEqual(self.uploads(), before_uploads)
        self.assertEqual(self.prices("2025-02"), before_prices)
        self.assertEqual(self.index_rows(), before_index)

    def test_rereport_source_upload_recorded_on_index(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 3.0)]:
            self.upload(month, [("A", "土豆", price)])
        self.upload("2025-02", [("A", "土豆", 2.0)])  # 内容相同的重报
        rows = self.index_rows()
        self.assertEqual(rows["2025-01"]["source_upload_id"], 1)
        self.assertEqual(rows["2025-02"]["source_upload_id"], 4)
        self.assertEqual(rows["2025-03"]["source_upload_id"], 3)
        self.assertEqual(rows["2025-02"]["pair_count"], 1)


class TestConsistency(Base):
    def test_index_query_does_not_read_price_details(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        baseline = self.table()
        conn = self.conn()
        try:
            # 直接篡改明细，模拟「明细不可信」的情形
            conn.execute("UPDATE prices SET price = 99 WHERE month='2025-02'")
        finally:
            conn.close()
        self.assertEqual(self.table(), baseline)

    def test_failed_write_rolls_back_prices_and_index(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        before = self.table()
        conn = self.conn()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # 触发器令写入价格失败
            conn.execute(
                "CREATE TRIGGER boom BEFORE INSERT ON prices "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        finally:
            conn.close()
        # 用新增行触发插入；改价走 UPDATE，不会碰到 INSERT 触发器
        resp = self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 9.0)])
        self.assertIn("回滚", resp.get_data(as_text=True))
        self.assertEqual(self.table(), before)
        conn = self.conn()
        try:
            price = conn.execute(
                "SELECT price FROM prices WHERE month='2025-02'"
            ).fetchone()["price"]
            uploads = conn.execute(
                "SELECT COUNT(*) c FROM uploads WHERE month='2025-02'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(price, 2.0)
        self.assertEqual(uploads, 1)


class TestDemo(Base):
    def test_demo_break_and_backfill(self):
        self.client.post("/demo/load", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-04"]["status"], "缺月")
        self.assertEqual(t["2025-05"]["status"], "断链")
        self.client.post("/demo/backfill", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-05"]["status"], "正常")
        self.client.post("/demo/break", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-05"]["index"])
        self.client.post("/demo/repair", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-05"]["status"], "正常")

    def test_demo_correction_updates_downstream(self):
        self.client.post("/demo/load", follow_redirects=True)
        self.client.post("/demo/backfill", follow_redirects=True)
        before = self.table()
        self.client.post("/demo/correct", follow_redirects=True)
        after = self.table()
        self.assertEqual(before["2025-01"], after["2025-01"])
        self.assertNotEqual(before["2025-02"]["factor"], after["2025-02"]["factor"])
        self.assertNotEqual(before["2025-05"]["index"], after["2025-05"]["index"])

    def test_demo_one_sided_reupload_only_changes_one_factor(self):
        self.client.post("/demo/load", follow_redirects=True)
        self.client.post("/demo/backfill", follow_redirects=True)
        before = self.table()
        self.client.post("/demo/one_sided", follow_redirects=True)
        after = self.table()
        # 2 月环比变了（对 1 月的配对里含 C-01 菠菜）
        self.assertNotEqual(before["2025-02"]["factor"], after["2025-02"]["factor"])
        # 3 月环比不变：3 月报价里没有 C-01 菠菜
        self.assertEqual(before["2025-03"]["factor"], after["2025-03"]["factor"])
        self.assertNotEqual(before["2025-02"]["index"], after["2025-02"]["index"])
        self.assertNotEqual(before["2025-05"]["index"], after["2025-05"]["index"])


class TestConcurrency(Base):
    def test_reader_never_sees_torn_snapshot(self):
        import threading

        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            try:
                while not stop.is_set():
                    i += 1
                    # 两个候选版本交替替换：环比 2.0 与 3.0
                    price = 2.0 if i % 2 else 3.0
                    self.upload("2025-02", [("A", "土豆", price)])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(80):
                rows = self.table()
                factor = rows["2025-02"]["factor"]
                index = rows["2025-02"]["index"]
                # 环比与指数必须来自同一次提交
                self.assertAlmostEqual(index, round(100 * factor, 6), places=4)
                self.assertIn(round(factor, 4), (2.0, 3.0))
        finally:
            stop.set()
            thread.join(timeout=20)
        self.assertEqual(errors, [])

    def test_interleaved_reuploads_apply_latest_state(self):
        """并发重报交错时，每批差异都相对「本次生效前」已提交的报价计算。"""
        import threading

        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])

        versions = {
            "x.csv": [("A", "土豆", 2.0), ("B", "白菜", 5.0), ("C", "菠菜", 8.0)],
            "y.csv": [("A", "土豆", 3.0), ("B", "白菜", 5.0), ("D", "黄瓜", 6.0)],
        }
        errors = []

        def writer(name):
            conn = self.conn()
            try:
                for _ in range(10):
                    services.process_upload(
                        conn, csv_bytes("2025-02", versions[name]), name
                    )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=writer, args=(n,)) for n in versions]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])

        conn = self.conn()
        try:
            batches = conn.execute(
                "SELECT id, filename, row_count, added_count, changed_count,"
                " removed_count, unchanged_count"
                " FROM uploads WHERE month='2025-02' ORDER BY id"
            ).fetchall()
            # 从初始状态按批次顺序重放改动，应得到最终生效报价
            state = {("A", "土豆"): 2.0, ("B", "白菜"): 5.0}
            for b in batches:
                if b["filename"] not in versions:
                    continue  # 初始数据批次
                file_rows = {
                    (stall, item): price
                    for stall, item, price in versions[b["filename"]]
                }
                changes = conn.execute(
                    "SELECT stall, item, change_type, old_price, new_price"
                    " FROM price_changes WHERE upload_id = ?",
                    (b["id"],),
                ).fetchall()
                unchanged = len(
                    {
                        key
                        for key in file_rows.keys() & state.keys()
                        if file_rows[key] == state[key]
                    }
                )
                for c in changes:
                    key = (c["stall"], c["item"])
                    if c["change_type"] == "remove":
                        self.assertEqual(c["old_price"], state.pop(key))
                    else:
                        if c["change_type"] == "change":
                            self.assertEqual(c["old_price"], state[key])
                        state[key] = c["new_price"]
                # 本批改动作用在「本批生效前」的状态上，恰好得到本批文件内容
                self.assertEqual(state, file_rows)
                self.assertEqual(b["unchanged_count"], unchanged)
                self.assertEqual(
                    b["added_count"] + b["changed_count"] + b["unchanged_count"],
                    b["row_count"],
                )
            final = {
                (r["stall"], r["item"]): r["price"]
                for r in conn.execute(
                    "SELECT stall, item, price FROM prices WHERE month='2025-02'"
                ).fetchall()
            }
        finally:
            conn.close()
        self.assertEqual(state, final)


if __name__ == "__main__":
    unittest.main(verbosity=2)
