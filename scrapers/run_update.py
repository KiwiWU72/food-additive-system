"""
run_update.py — 食品添加物合規系統：每月法規更新執行器
=========================================================
功能：
  1. 讀取現有 db_compact.json（804 筆台灣添加物資料庫）
  2. 依指定的爬蟲（US/EU/全部）對目標法規資料庫進行抓取
  3. 將爬蟲回傳的 patch 深度合併回資料庫
  4. 輸出更新後的 JSON 及變更報告

執行方式：

  # 只更新 US FDA 資料
  python run_update.py --scraper us --input db_compact.json --output db_updated.json

  # 只更新 EU EFSA 資料
  python run_update.py --scraper eu --input db_compact.json --output db_updated.json

  # 更新全部（US + EU）
  python run_update.py --scraper all --input db_compact.json --output db_updated.json

  # 僅更新指定 additives（以 ID 篩選，逗號分隔）
  python run_update.py --scraper us --ids INS-102,INS-171,TW-496

  # 不執行爬蟲，只列出需人工審核的項目
  python run_update.py --review-only --input db_compact.json

  # 除錯模式（顯示瀏覽器視窗）
  python run_update.py --scraper us --headless false

輸出檔案：
  db_updated.json      — 更新後的完整資料庫（可直接替換 db_compact.json）
  patch_YYYYMMDD.json  — 本次爬蟲產生的 patch 清單（可用於 git diff 比較）
  report_YYYYMMDD.txt  — 純文字變更報告（含 needs_manual_review 清單）

安裝需求：
  pip install playwright structlog --break-system-packages
  playwright install chromium
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

# ── 設定日誌 ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(f"update_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"),
    ],
)
logger = logging.getLogger("run_update")


# ═══════════════════════════════════════════════════════════════════════════
# Deep Merge 工具函式（與前端 mergeAdditives() 邏輯對應）
# ═══════════════════════════════════════════════════════════════════════════

def deep_merge(base: dict, patch: dict) -> dict:
    """
    深度合併兩個 dict。patch 的值覆蓋 base，
    但若兩者的值都是 dict，則遞迴合併（而非整個覆蓋）。

    這確保爬蟲只更新它抓到的欄位，不會清空其他欄位。

    範例：
        base  = {"US": {"permitted": True,  "cfrRef": "21 CFR 182.x", "notes": "old"}}
        patch = {"US": {"permitted": True,  "source_url": "https://...", "last_scraped_at": "2025-06-01"}}
        結果  = {"US": {"permitted": True,  "cfrRef": "21 CFR 182.x", "notes": "old",
                         "source_url": "https://...", "last_scraped_at": "2025-06-01"}}
    """
    result = copy.deepcopy(base)
    for key, val in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def apply_patches(database: list[dict], patches: list[dict]) -> tuple[list[dict], list[str]]:
    """
    將 patch 清單應用到 database。

    Args:
        database: db_compact.json 中的 additives 清單
        patches:  爬蟲回傳的 patch 清單

    Returns:
        (updated_database, change_log)
        change_log 是人類可讀的變更說明清單
    """
    # 建立 ID -> index 索引（O(1) 查找）
    id_index: dict[str, int] = {a["id"]: i for i, a in enumerate(database)}
    updated = copy.deepcopy(database)
    change_log: list[str] = []

    for patch in patches:
        pid = patch.get("id")
        if not pid:
            logger.warning(f"Patch 沒有 id 欄位，略過：{patch}")
            continue

        if pid not in id_index:
            logger.warning(f"資料庫中找不到 id={pid}，略過此 patch")
            change_log.append(f"  [SKIP] {pid} — 資料庫中不存在")
            continue

        idx = id_index[pid]
        old_reg = updated[idx].get("regulations", {})
        patch_reg = patch.get("regulations", {})

        # 逐國合併 regulations
        new_reg = deep_merge(old_reg, patch_reg)
        updated[idx]["regulations"] = new_reg

        # 記錄變更
        for country, reg_data in patch_reg.items():
            if isinstance(reg_data, dict):
                needs_review = reg_data.get("needs_manual_review", False)
                permitted = reg_data.get("permitted")
                status = "⚠️ 需審核" if needs_review else ("✅ 已更新" if permitted else "❌ 不允許")
                url = reg_data.get("source_url", "—")[:60]
                change_log.append(
                    f"  [{status}] {pid:15s} | {country} | permitted={str(permitted):5s} | {url}"
                )

    return updated, change_log


# ═══════════════════════════════════════════════════════════════════════════
# 爬蟲工廠
# ═══════════════════════════════════════════════════════════════════════════

def get_scrapers(scraper_name: str, headless: bool, concurrency: int) -> list:
    """
    依名稱建立爬蟲實例。

    Args:
        scraper_name: "us", "eu", 或 "all"
        headless:     是否無頭模式
        concurrency:  並行分頁數

    Returns:
        爬蟲實例清單
    """
    scrapers = []
    name = scraper_name.lower()

    if name in ("us", "all"):
        try:
            from ecfr_scraper import EcfrScraper
            scrapers.append(("US", EcfrScraper(headless=headless, concurrency=concurrency)))
            logger.info("已載入 EcfrScraper（US FDA）")
        except ImportError as e:
            logger.error(f"無法載入 ecfr_scraper.py：{e}")
            sys.exit(1)

    if name in ("eu", "all"):
        try:
            from efsa_scraper import EfsaScraper
            scrapers.append(("EU", EfsaScraper(headless=headless, concurrency=concurrency)))
            logger.info("已載入 EfsaScraper（EU EFSA）")
        except ImportError as e:
            logger.error(f"無法載入 efsa_scraper.py：{e}")
            sys.exit(1)

    if not scrapers:
        logger.error(f"未知的爬蟲名稱：{scraper_name}（使用 us / eu / all）")
        sys.exit(1)

    return scrapers


# ═══════════════════════════════════════════════════════════════════════════
# Review 報告生成
# ═══════════════════════════════════════════════════════════════════════════

def generate_review_report(database: list[dict], country: Optional[str] = None) -> str:
    """
    掃描資料庫，列出所有 needs_manual_review=True 的項目。

    Args:
        database: additives 清單
        country:  若指定，只列出此國家的審核項目（None = 全部）

    Returns:
        純文字報告字串
    """
    lines = [
        "=" * 70,
        f"食品添加物合規系統 — 人工審核清單",
        f"產生時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 70,
        "",
    ]

    review_items: list[tuple[str, str, str, str]] = []  # (id, nameTW, country, reason)

    for a in database:
        regulations = a.get("regulations", {})
        for c, reg in regulations.items():
            if not isinstance(reg, dict):
                continue
            if country and c != country:
                continue
            if reg.get("needs_manual_review"):
                review_items.append((
                    a.get("id", "?"),
                    a.get("nameTW", ""),
                    c,
                    reg.get("review_reason", reg.get("notes", "（原因未記錄）")),
                ))

    if not review_items:
        lines.append("✅ 目前沒有需要人工審核的項目。")
    else:
        lines.append(f"⚠️  共 {len(review_items)} 個項目需要人工審核：\n")
        for i, (aid, name_tw, c, reason) in enumerate(review_items, 1):
            lines.append(f"  {i:3d}. [{c}] {aid:15s} {name_tw[:20]:20s}")
            lines.append(f"       原因：{reason[:100]}")
            lines.append("")

    lines.append("=" * 70)
    lines.append("月度更新建議：")
    lines.append("  1. 請逐一核對上列項目的最新官方公告")
    lines.append("  2. 更新 db_compact.json 中對應的 regulations 欄位")
    lines.append("  3. 將 needs_manual_review 改為 false 並記錄 source_url")
    lines.append("  4. 重要變更（撤銷/禁用）請同步更新 food-additive-compliance-v3.html")
    lines.append("=" * 70)

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# 主程式
# ═══════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="食品添加物合規系統 — 月度法規更新工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python run_update.py --scraper us --input db_compact.json
  python run_update.py --scraper eu --ids E102,E171,E124
  python run_update.py --scraper all --concurrency 2
  python run_update.py --review-only
        """,
    )
    parser.add_argument(
        "--scraper", choices=["us", "eu", "all"], default="all",
        help="要執行的爬蟲（us=FDA, eu=EFSA, all=兩者）",
    )
    parser.add_argument(
        "--input", default="db_compact.json",
        help="輸入的資料庫 JSON 路徑（預設：db_compact.json）",
    )
    parser.add_argument(
        "--output", default=None,
        help="輸出的資料庫 JSON 路徑（預設：db_compact_updated_YYYYMMDD.json）",
    )
    parser.add_argument(
        "--ids", default=None,
        help="只更新這些 ID（逗號分隔，例：INS-102,INS-171,TW-496）",
    )
    parser.add_argument(
        "--headless", choices=["true", "false"], default="true",
        help="是否使用無頭瀏覽器（預設：true；false=顯示視窗，用於除錯）",
    )
    parser.add_argument(
        "--concurrency", type=int, default=3,
        help="同時執行的分頁數量（預設：3，建議 ≤ 5 避免被封鎖）",
    )
    parser.add_argument(
        "--review-only", action="store_true",
        help="只列出目前 needs_manual_review=True 的項目，不執行爬蟲",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="模擬執行：顯示會抓取哪些項目，但不實際爬取",
    )
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> None:
    today = datetime.now().strftime("%Y%m%d")

    # ── 讀取資料庫 ─────────────────────────────────────────────────────
    input_path = Path(args.input)
    if not input_path.exists():
        # 嘗試在上層目錄找
        alt_path = Path("..") / args.input
        if alt_path.exists():
            input_path = alt_path
        else:
            logger.error(f"找不到輸入檔案：{args.input}")
            sys.exit(1)

    logger.info(f"讀取資料庫：{input_path} ...")
    with open(input_path, encoding="utf-8") as f:
        db_data = json.load(f)

    # 支援兩種結構：直接陣列 或 { "additives": [...] }
    if isinstance(db_data, list):
        additives: list[dict] = db_data
        db_wrapper: Optional[dict] = None
    else:
        additives = db_data.get("additives", [])
        db_wrapper = db_data

    logger.info(f"讀取完成：共 {len(additives)} 筆添加物資料")

    # ── --review-only 模式 ─────────────────────────────────────────────
    if args.review_only:
        report = generate_review_report(additives)
        print(report)
        report_path = f"review_report_{today}.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
        logger.info(f"審核報告已儲存：{report_path}")
        return

    # ── 篩選目標 additives ─────────────────────────────────────────────
    target_additives: list[dict]
    if args.ids:
        target_ids = {i.strip() for i in args.ids.split(",")}
        target_additives = [a for a in additives if a.get("id") in target_ids]
        missing = target_ids - {a.get("id") for a in target_additives}
        if missing:
            logger.warning(f"以下 ID 在資料庫中不存在：{missing}")
        logger.info(f"篩選後目標：{len(target_additives)} 筆")
    else:
        target_additives = additives

    # ── --dry-run 模式 ─────────────────────────────────────────────────
    if args.dry_run:
        print(f"\n=== Dry Run：將抓取 {len(target_additives)} 筆添加物 ===")
        for a in target_additives[:20]:
            print(
                f"  {a.get('id'):15s} | CAS={a.get('casNumber',''):15s} | "
                f"E={a.get('eNumber',''):8s} | {a.get('nameTW','')[:20]}"
            )
        if len(target_additives) > 20:
            print(f"  ... 以及另外 {len(target_additives) - 20} 筆")
        return

    # ── 執行爬蟲 ─────────────────────────────────────────────────────
    headless = args.headless.lower() != "false"
    scrapers = get_scrapers(args.scraper, headless=headless, concurrency=args.concurrency)

    all_patches: list[dict] = []

    for country_code, scraper in scrapers:
        logger.info(f"\n{'='*60}")
        logger.info(f"開始執行 {scraper.SCRAPER_NAME}（{country_code}）...")
        logger.info(f"目標：{len(target_additives)} 筆添加物")
        logger.info(f"{'='*60}")

        try:
            patches = await scraper.run(target_additives)
            all_patches.extend(patches)
            logger.info(f"{country_code} 爬蟲完成：產生 {len(patches)} 個 patch")
        except Exception as e:  # noqa: BLE001
            logger.error(f"{country_code} 爬蟲發生例外：{e}", exc_info=True)
            # 繼續執行其他爬蟲

    # ── 儲存 patch 檔 ─────────────────────────────────────────────────
    patch_path = f"patch_{today}.json"
    with open(patch_path, "w", encoding="utf-8") as f:
        json.dump(all_patches, f, ensure_ascii=False, indent=2)
    logger.info(f"Patch 已儲存：{patch_path}（{len(all_patches)} 個更新）")

    # ── 應用 patches 到資料庫 ─────────────────────────────────────────
    logger.info("應用 patches 到資料庫...")
    updated_additives, change_log = apply_patches(additives, all_patches)

    # ── 組裝最終輸出 ──────────────────────────────────────────────────
    if db_wrapper is not None:
        output_data = {**db_wrapper, "additives": updated_additives}
    else:
        output_data = updated_additives

    # ── 儲存更新後的資料庫 ────────────────────────────────────────────
    output_path = args.output or f"db_compact_updated_{today}.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_data, f, ensure_ascii=False, separators=(",", ":"))
    logger.info(f"更新後資料庫已儲存：{output_path}")

    # ── 產生審核報告 ──────────────────────────────────────────────────
    report = generate_review_report(updated_additives)
    report_path = f"report_{today}.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    # ── 列印變更摘要 ──────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"更新完成！共處理 {len(target_additives)} 筆，產生 {len(all_patches)} 個 patch")
    print(f"{'='*70}")
    if change_log:
        print("\n變更清單（前 30 筆）：")
        for line in change_log[:30]:
            print(line)
        if len(change_log) > 30:
            print(f"  ... 以及另外 {len(change_log) - 30} 筆（詳見 {report_path}）")

    print(f"\n輸出檔案：")
    print(f"  📄 更新資料庫：{output_path}")
    print(f"  🔧 Patch 記錄：{patch_path}")
    print(f"  📋 審核報告：{report_path}")
    print(f"\n{'='*70}")

    # 顯示需審核數量
    review_count = sum(
        1 for p in all_patches
        for reg in (p.get("regulations") or {}).values()
        if isinstance(reg, dict) and reg.get("needs_manual_review")
    )
    if review_count:
        print(f"⚠️  {review_count} 個項目需要人工審核，請查看 {report_path}")
    else:
        print("✅ 所有項目均已自動更新，無需人工審核")


def main() -> None:
    args = parse_args()

    # 輸出執行環境資訊
    logger.info("=" * 60)
    logger.info("食品添加物合規系統 — 月度法規更新工具")
    logger.info(f"日期：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"爬蟲：{args.scraper} | 無頭：{args.headless} | 並行：{args.concurrency}")
    logger.info("=" * 60)

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logger.info("使用者中斷執行（Ctrl+C）")
        sys.exit(0)
    except Exception as e:
        logger.error(f"執行失敗：{e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
