"""报价 CSV 解析与重报业务逻辑。

重报是增量写入：整份 CSV 仍然上传，但落库时按 (摊位, 品种) 与本次生效前的
最新报价比对，只有新增、改价、撤下的行会触发报价值写入与环比重算。
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from . import calc, db

MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

HEADER_ALIASES = {
    "month": {"month", "月份", "年月", "报价月份"},
    "stall": {"stall", "摊位", "摊位号", "摊主"},
    "item": {"item", "品种", "蔬菜品种", "菜品"},
    "price": {"price", "价格", "报价", "每公斤报价", "单价", "元/公斤"},
}

MAX_ROWS = 20000
MAX_MONTHS = 600  # 基月到最新报价月最多 50 年


class UploadError(Exception):
    """可直接展示给用户的校验错误。"""

    def __init__(self, message: str, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


@dataclass
class ParsedRow:
    line: int
    month: str
    stall: str
    item: str
    price: float


@dataclass
class ParseResult:
    month: str
    rows: list[ParsedRow] = field(default_factory=list)


def decode_csv(raw: bytes) -> str:
    if not raw:
        raise UploadError("文件为空")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise UploadError("无法识别文件编码，请另存为 UTF-8 编码的 CSV")


def _normalize_header(name: str) -> str:
    return (name or "").strip().lstrip("﻿")


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for raw in fieldnames:
        key = _normalize_header(raw).lower()
        for canonical, aliases in HEADER_ALIASES.items():
            if key in aliases:
                mapping[canonical] = raw
                break
    missing = [c for c in ("month", "stall", "item", "price") if c not in mapping]
    if missing:
        zh = {"month": "月份", "stall": "摊位", "item": "品种", "price": "价格"}
        raise UploadError(
            "CSV 缺少必需列：" + "、".join(zh[c] for c in missing),
            ["表头需包含：月份, 摊位, 品种, 价格（或每公斤报价）"],
        )
    return mapping


def _parse_price(text: str, line: int) -> float:
    value = (text or "").strip().replace("￥", "").replace("¥", "").replace(",", "")
    if not value:
        raise UploadError(f"第 {line} 行：价格为空")
    try:
        price = float(value)
    except ValueError:
        raise UploadError(f"第 {line} 行：价格「{text.strip()}」不是数字") from None
    if price != price or price in (float("inf"), float("-inf")):
        raise UploadError(f"第 {line} 行：价格「{text.strip()}」不是有效数字")
    if price <= 0:
        raise UploadError(f"第 {line} 行：价格必须大于 0，实际为 {text.strip()}")
    return price


def parse_csv(raw: bytes) -> ParseResult:
    text = decode_csv(raw)
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""))
        if not reader.fieldnames:
            raise UploadError("CSV 没有表头")
        mapping = _map_headers(list(reader.fieldnames))
        rows: list[ParsedRow] = []
        seen: dict[tuple[str, str], int] = {}
        duplicates: list[str] = []
        bad_months: list[str] = []
        months: set[str] = set()

        for offset, rec in enumerate(reader, start=2):
            if None in rec:
                raise UploadError(f"第 {offset} 行：列数与表头不一致")
            values = [ (rec.get(col) or "").strip() for col in rec ]
            if not any(values):
                continue  # 跳过完全空行
            month = (rec.get(mapping["month"]) or "").strip()
            stall = (rec.get(mapping["stall"]) or "").strip()
            item = (rec.get(mapping["item"]) or "").strip()
            price_text = rec.get(mapping["price"]) or ""

            if not MONTH_RE.match(month):
                bad_months.append(f"第 {offset} 行：月份「{month}」应为 YYYY-MM")
                continue
            if not stall:
                raise UploadError(f"第 {offset} 行：摊位为空")
            if not item:
                raise UploadError(f"第 {offset} 行：品种为空")
            price = _parse_price(price_text, offset)

            key = (stall, item)
            if key in seen:
                duplicates.append(
                    f"摊位「{stall}」品种「{item}」出现在第 {seen[key]} 行和第 {offset} 行"
                )
                continue
            seen[key] = offset
            months.add(month)
            rows.append(ParsedRow(offset, month, stall, item, price))

            if len(rows) > MAX_ROWS:
                raise UploadError(f"单次上传不能超过 {MAX_ROWS} 条报价")
    except csv.Error as exc:
        raise UploadError(f"CSV 解析失败：{exc}") from None

    if bad_months:
        raise UploadError("部分行月份格式不正确", bad_months[:20])
    if duplicates:
        raise UploadError(
            "同一摊位同一品种出现多条报价（同月同摊同品种只收一条）",
            duplicates[:20],
        )
    if not rows:
        raise UploadError("文件中没有有效报价")
    if len(months) > 1:
        raise UploadError(
            "一次只能上传一个月的报价，文件中包含：" + "、".join(sorted(months)),
        )

    return ParseResult(month=rows[0].month, rows=rows)


def base_month(conn) -> str | None:
    return db.get_meta(conn, "base_month")


def process_upload(conn, raw: bytes, filename: str) -> dict:
    """解析 + 增量重报 + 重算指数，单事务完成。

    重报时按 (摊位, 品种) 与「本次生效前」的最新报价比对，价格表只写入新增、
    改价、撤下的行，未变行一个字段都不动。整月报价仍归入新批次：批次记录的
    row_count 是整月有效行数，指数结果的 source_upload_id 指向该批次。
    即使内容完全相同也会登记新批次（增/改/删均为 0）。
    """
    parsed = parse_csv(raw)
    month = parsed.month

    with db.transaction(conn):
        existing_base = base_month(conn)
        if existing_base is None:
            new_base = month
        else:
            new_base = existing_base
            if month < new_base:
                raise UploadError(
                    f"{month} 早于基月 {new_base}，基月之前的报价不接收"
                )
        latest = max(filter(None, (month, db.max_uploaded_month(conn))))
        if len(db.month_range(new_base, latest)) > MAX_MONTHS:
            raise UploadError(f"基月到最新报价月的跨度不能超过 {MAX_MONTHS} 个月")

        # 事务开始后读取：并发重报被 BEGIN IMMEDIATE 串行化，这里拿到的是
        # 本次生效前最后一次已提交的报价。
        had_upload = db.month_upload_count(conn, month) > 0
        old_prices = db.month_prices(conn, month)
        new_prices = {(r.stall, r.item): r.price for r in parsed.rows}

        added_keys = new_prices.keys() - old_prices.keys()
        removed_keys = old_prices.keys() - new_prices.keys()
        changed_keys = {
            key
            for key in new_prices.keys() & old_prices.keys()
            if new_prices[key] != old_prices[key]
        }
        unchanged_keys = new_prices.keys() - added_keys - changed_keys

        # 本月环比（对前月）与下月环比（对本月）分别判断：只有可比报价对
        # 增减、或配对中任一价格变化，才重算那一段。本月由「缺月」变为
        # 「有报价」时，即使凑不出可比报价对，也要把 0 对/空环比落库。
        factor_months: set[str] = set()
        if existing_base is not None:
            prev_prices = db.month_prices(conn, db.previous_month(month))
            if month > new_base and (
                not had_upload
                or calc.pairing_changed(old_prices, new_prices, prev_prices)
            ):
                factor_months.add(month)
            next_month = db.next_month(month)
            next_prices = db.month_prices(conn, next_month)
            if next_month > new_base and calc.pairing_changed(
                old_prices, new_prices, next_prices
            ):
                factor_months.add(next_month)

        cur = conn.execute(
            "INSERT INTO uploads"
            "(month, filename, row_count, uploaded_at, is_base_month, "
            " added_count, changed_count, removed_count, unchanged_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                month,
                filename or "upload.csv",
                len(parsed.rows),
                calc.now_iso(),
                1 if month == new_base else 0,
                len(added_keys),
                len(changed_keys),
                len(removed_keys),
                len(unchanged_keys),
            ),
        )
        upload_id = cur.lastrowid

        # 报价值写入严格限定为增、改、删三类行。
        conn.executemany(
            "INSERT INTO prices(upload_id, month, stall, item, price) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (upload_id, month, stall, item, new_prices[(stall, item)])
                for stall, item in added_keys
            ],
        )
        conn.executemany(
            "UPDATE prices SET price = ?, upload_id = ? "
            "WHERE month = ? AND stall = ? AND item = ?",
            [
                (new_prices[key], upload_id, month, key[0], key[1])
                for key in changed_keys
            ],
        )
        conn.executemany(
            "DELETE FROM prices WHERE month = ? AND stall = ? AND item = ?",
            [(month, stall, item) for stall, item in removed_keys],
        )
        # 报价值与价格行的写入严格限定为增、改、删三类行：未变行一个字段都
        # 不重写。整月报价归新批次记在批次表：本批次的 row_count 即整月有效
        # 行数，指数结果的 source_upload_id 也指向本批次。

        db.insert_price_changes(
            conn,
            upload_id,
            month,
            [
                {
                    "stall": stall,
                    "item": item,
                    "change_type": "add",
                    "old_price": None,
                    "new_price": new_prices[(stall, item)],
                }
                for stall, item in sorted(added_keys)
            ]
            + [
                {
                    "stall": stall,
                    "item": item,
                    "change_type": "change",
                    "old_price": old_prices[(stall, item)],
                    "new_price": new_prices[(stall, item)],
                }
                for stall, item in sorted(changed_keys)
            ]
            + [
                {
                    "stall": stall,
                    "item": item,
                    "change_type": "remove",
                    "old_price": old_prices[(stall, item)],
                    "new_price": None,
                }
                for stall, item in sorted(removed_keys)
            ],
        )

        if existing_base is None:
            db.set_meta(conn, "base_month", new_base)

        calc.recompute(
            conn,
            new_base,
            factor_months=None if existing_base is None else factor_months,
            latest_month=latest,
        )

    return {
        "month": month,
        "rows": len(parsed.rows),
        "upload_id": upload_id,
        "base_month": new_base,
        "replaced": existing_base is not None,
        "added": len(added_keys),
        "changed": len(changed_keys),
        "removed": len(removed_keys),
        "unchanged": len(unchanged_keys),
        "recomputed": sorted(factor_months),
        "full_recompute": existing_base is None,
    }
