"""按当前价表重算历史 usage 的 cost（元）。

早期版本只记上游积分，没有成本列；从积分口径切到人民币口径时，
老记录的 cost 是空的，成员详情页会显示成 ¥0，看起来像没消耗。
这个脚本把「有 token 但没成本」的行按当前价表补一次。

可重复执行：只处理 cost 为 0 且有 token 的行，跑过的行不会再被改。
用法：
    python3 tools/backfill_cost.py            # 干跑，只报数
    python3 tools/backfill_cost.py --apply    # 实际写入
"""

from __future__ import annotations

import os
import sqlite3
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE, "src"))

import pricing  # noqa: E402

DB_PATH = os.environ.get("PMT_DB") or os.path.join(BASE, "data", "oh-pmt-api.db")


def main() -> int:
    apply = "--apply" in sys.argv
    if not os.path.exists(DB_PATH):
        print(f"数据库不存在：{DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT id, model, prompt_tokens, completion_tokens,
                  cache_read_tokens, cache_write_tokens
             FROM usage WHERE COALESCE(cost,0)=0 AND total_tokens>0"""
    ).fetchall()
    print(f"数据库：{DB_PATH}")
    print(f"待回填：{len(rows)} 行")
    if not rows:
        print("没有需要回填的记录。")
        return 0

    total = 0.0
    unknown: dict[str, int] = {}
    updates = []
    for r in rows:
        price, source = pricing.lookup(r["model"], overrides={})
        if source == "default":
            unknown[r["model"]] = unknown.get(r["model"], 0) + 1
        cost = pricing.cost_of(
            price,
            prompt_tokens=r["prompt_tokens"] or 0,
            completion_tokens=r["completion_tokens"] or 0,
            cache_read_tokens=r["cache_read_tokens"] or 0,
            cache_write_tokens=r["cache_write_tokens"] or 0,
        )
        total += cost
        if cost > 0:
            updates.append((cost, f"backfill:{source}"[:16], r["id"]))

    print(f"回填后新增总额：¥{total:.4f}")
    if unknown:
        print("以下模型不在价表里，按默认价估算：")
        for model, n in sorted(unknown.items(), key=lambda kv: -kv[1]):
            print(f"  {model}  {n} 行")

    if not apply:
        print("\n这是干跑。要写入请加 --apply")
        return 0

    conn.executemany(
        "UPDATE usage SET cost=?, price_source=? WHERE id=?", updates)
    conn.commit()
    left = conn.execute(
        """SELECT COUNT(*) FROM usage
            WHERE COALESCE(cost,0)=0 AND total_tokens>0"""
    ).fetchone()[0]
    got = conn.execute("SELECT COALESCE(SUM(cost),0) FROM usage").fetchone()[0]
    print(f"已写入 {len(updates)} 行，全库成本合计 ¥{got:.4f}")
    print(f"仍缺成本的行：{left}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
