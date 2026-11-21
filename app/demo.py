"""内置演示数据：一键复现「缺月 → 断链 → 补报接续 / 更正恢复 / 单侧重算」。

演示数据直接以 CSV 文本生成，再走与网页上传完全相同的业务路径，
因此演示结果与真实上传一致。
"""

from __future__ import annotations

import csv
import io

from . import db, services

MONTHS = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"]

# 每月相对基月的价格倍率
SCALE = {
    "2025-01": 1.00,
    "2025-02": 1.05,
    "2025-03": 1.12,
    "2025-04": 1.09,
    "2025-05": 1.18,
}

BASE_PRICES = {
    ("A-01", "大白菜"): 4.00,
    ("A-01", "土豆"): 3.00,
    ("A-02", "大白菜"): 5.00,
    ("A-02", "西红柿"): 7.00,
    ("B-01", "土豆"): 3.00,
    ("B-01", "西红柿"): 6.00,
    # 只在 1、2 月出现的摊位品种：用来演示「重报只影响一侧环比」
    ("C-01", "菠菜"): 8.00,
}

ONE_SIDED = ("C-01", "菠菜")
ONE_SIDED_MONTHS = {"2025-01", "2025-02"}

BREAK_PRICES = {
    ("X-01", "大白菜"): 5.10,
    ("X-01", "土豆"): 4.05,
    ("Y-01", "西红柿"): 7.25,
}



class DemoError(Exception):
    pass


def _price(stall: str, item: str, month: str) -> float | None:
    base = BASE_PRICES.get((stall, item))
    if base is None:
        return None
    if (stall, item) == ONE_SIDED and month not in ONE_SIDED_MONTHS:
        return None
    return round(base * SCALE[month], 2)


def build_csv(month: str, layout: str = "normal") -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["月份", "摊位", "品种", "每公斤报价"])
    if layout == "normal":
        source = {
            (stall, item): _price(stall, item, month)
            for (stall, item) in BASE_PRICES
        }
    else:
        source = dict(BREAK_PRICES)
    for (stall, item), price in source.items():
        if price is None:
            continue
        writer.writerow([month, stall, item, f"{price:.2f}"])
    return buf.getvalue()


def _upload(conn, month: str, layout: str = "normal") -> None:
    raw = build_csv(month, layout).encode("utf-8")
    services.process_upload(conn, raw, f"demo-{month}-{layout}.csv")


def _current_csv(conn, month: str) -> str:
    """按数据库里该月当前生效的报价重新生成 CSV（用于局部更正演示）。"""
    prices = db.month_prices(conn, month)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["月份", "摊位", "品种", "每公斤报价"])
    for (stall, item), price in sorted(prices.items()):
        writer.writerow([month, stall, item, f"{price:.2f}"])
    return buf.getvalue()


def reset(conn) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM price_changes")
        conn.execute("DELETE FROM prices")
        conn.execute("DELETE FROM index_months")
        conn.execute("DELETE FROM uploads")
        conn.execute("DELETE FROM meta")
        conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                     "('prices','uploads','price_changes')")


def _require_demo(conn) -> None:
    base = services.base_month(conn)
    if base is None:
        raise DemoError("请先点「载入示例数据」")
    if base != MONTHS[0]:
        raise DemoError("当前数据不是示例数据，请先点「载入示例数据」")
    have = {
        r["month"]
        for r in conn.execute("SELECT DISTINCT month FROM uploads").fetchall()
    }
    if "2025-02" not in have or "2025-03" not in have:
        raise DemoError("示例数据不完整，请重新载入")


def run(action: str, conn) -> str:
    if action == "reset":
        reset(conn)
        return "演示数据已清空，可重新载入示例。"

    if action == "load":
        reset(conn)
        for month in MONTHS:
            if month == "2025-04":
                continue  # 故意缺 4 月
            _upload(conn, month)
        return "已载入 2025 年 1、2、3、5 月报价：4 月缺月，5 月因此断链。"

    _require_demo(conn)

    if action == "backfill":
        if conn.execute("SELECT 1 FROM uploads WHERE month='2025-04'").fetchone():
            raise DemoError("4 月已有报价，无需补报（可先点「载入示例数据」重置）")
        result = services.process_upload(
            conn, build_csv("2025-04").encode("utf-8"), "demo-2025-04-backfill.csv"
        )
        return (
            f"批次 #{result['upload_id']}：已补报 2025-04（新增 "
            f"{result['added']} 条报价）：4 月环比接通，5 月指数随之恢复。"
        )

    if action == "break":
        result = services.process_upload(
            conn,
            build_csv("2025-03", "break").encode("utf-8"),
            "demo-2025-03-alt-stalls.csv",
        )
        return (
            f"批次 #{result['upload_id']}：已把 2025-03 换成完全不同摊位的报价"
            f"（新增 {result['added']}、撤下 {result['removed']}）："
            "3 月起无可比报价对，指数清空。"
        )

    if action == "repair":
        result = services.process_upload(
            conn, build_csv("2025-03").encode("utf-8"), "demo-2025-03-repaired.csv"
        )
        return (
            f"批次 #{result['upload_id']}：已按原样重报 2025-03"
            f"（新增 {result['added']}、改价 {result['changed']}、"
            f"撤下 {result['removed']}）：3 月环比接通，后续指数重新接上。"
        )

    if action == "correct":
        # 只更正部分摊位：环比不会被整体抵消，后续指数会真实变化
        lines = _current_csv(conn, "2025-02").splitlines()
        rows = [lines[0]]
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = next(csv.reader([line]))
            if parts[1] == "A-01":
                parts[3] = f"{float(parts[3]) * 1.20:.2f}"
            rows.append(",".join(parts))
        raw = ("\n".join(rows) + "\n").encode("utf-8")
        result = services.process_upload(conn, raw, "demo-2025-02-corrected.csv")
        recalculated = "、".join(result["recomputed"]) or "无"
        return (
            f"批次 #{result['upload_id']}：2025-02 的 A-01 摊位报价上调 20%"
            f"（新增 {result['added']}、改价 {result['changed']}、"
            f"撤下 {result['removed']}）。重算的环比：{recalculated}；"
            "2 月及之后各月指数随之更新。"
        )

    if action == "one_sided":
        # 菠菜只在 1、2 月有报价：改它只会动 2 月环比（对 1 月），
        # 不会动 3 月环比（3 月没有该摊位品种）。
        lines = _current_csv(conn, "2025-02").splitlines()
        rows = [lines[0]]
        for line in lines[1:]:
            if not line.strip():
                continue
            parts = next(csv.reader([line]))
            if (parts[1], parts[2]) == ONE_SIDED:
                parts[3] = f"{float(parts[3]) * 1.50:.2f}"
            rows.append(",".join(parts))
        raw = ("\n".join(rows) + "\n").encode("utf-8")
        result = services.process_upload(conn, raw, "demo-2025-02-one-sided.csv")
        recalculated = "、".join(result["recomputed"]) or "无"
        return (
            f"批次 #{result['upload_id']}：2025-02 的 C-01 菠菜报价上调 50%"
            f"（新增 {result['added']}、改价 {result['changed']}、"
            f"撤下 {result['removed']}）。重算的环比：{recalculated}；"
            "C-01 菠菜不在 3 月报价中，3 月环比保持不变。"
        )

    raise DemoError("未知的演示操作")
