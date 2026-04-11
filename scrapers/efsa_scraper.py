"""
efsa_scraper.py — EFSA / EU 食品添加物法規爬蟲（以 E-Number 為索引鍵）
=======================================================================
目標網站：
  - EFSA 食品添加物資料庫（FoodEx2）：
    https://www.efsa.europa.eu/en/food-additives-flavourings-processing-aids/food-additives
  - EU 食品添加物資料庫（EUR-Lex Regulation 1333/2008 Annex II）：
    https://food.ec.europa.eu/food-safety/food-improvement-agents/additives_en
  - EFSA 評估報告搜尋：
    https://efsa.onlinelibrary.wiley.com/doi/search?query={E-number}

查詢流程：
  1. 以 E-Number 查詢 EU 食品添加物資料庫（food.ec.europa.eu）
  2. 取得允許食品類別、最大限量（Annex II 格式）
  3. 偵測 Southampton 警告（偶氮染料六種：E102, E104, E110, E122, E124, E129）
  4. 偵測 TiO2 (E171) 歐盟禁用狀態（2022年起）
  5. 寫入 source_url, last_scraped_at, needs_manual_review

EU 法規特殊原則（程式中需標記）：
  - QS (quantum satis)：「適量」，無最大限量，需在 notes 中標記
  - Carry-over：食品成分帶入、非直接添加，需在 notes 中標記
  - Annex III：加工助劑（processing aids），另行規範

每月更新：
  python run_update.py --scraper eu --input db_compact.json --output patch_eu.json
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Optional

from playwright.async_api import Page

from base_scraper import BaseScraper, flag_for_review, make_provenance

# ═══════════════════════════════════════════════════════════════════════════
# 常數
# ═══════════════════════════════════════════════════════════════════════════

COUNTRY_CODE = "EU"

# EU 食品添加物資料庫（歐盟委員會）
EU_DB_BASE = "https://food.ec.europa.eu/food-safety/food-improvement-agents/additives_en"

# EU 查詢端點（實際搜尋 URL，使用 E-number 過濾）
EU_SEARCH_URL = (
    "https://food.ec.europa.eu/food-safety/food-improvement-agents/additives_en"
    "#additives-list"
)

# EUR-Lex：Regulation (EC) No 1333/2008 全文
EURLEX_REG_URL = "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:02008R1333-20231208"

# EFSA 評估報告搜尋（以 E-number 查詢）
EFSA_SEARCH_URL = (
    "https://efsa.onlinelibrary.wiley.com/doi/search"
    "?query={enumber}&scope=site&contentTypeCode=JO"
)

# EFSA 食品添加物評估數據庫
EFSA_DB_URL = "https://www.efsa.europa.eu/en/food-additives-flavourings-processing-aids/food-additives"

# ── Southampton 六種偶氮染料（需加警告標示）──────────────────────────
# 依據：2008 McCann et al. 研究；EU Regulation (EC) No 1333/2008 Article 24
# 含這些 E-number 的食品（歐盟內銷售）必須標示：
#   "may have an adverse effect on activity and attention in children"
SOUTHAMPTON_SIX: set[str] = {
    "E102",  # Tartrazine（食用黃色四號）
    "E104",  # Quinoline Yellow（喹啉黃）
    "E110",  # Sunset Yellow FCF（食用黃色五號）
    "E122",  # Carmoisine / Azorubine（偶氮玉紅）
    "E124",  # Ponceau 4R（食用紅色六號）
    "E129",  # Allura Red AC（食用紅色七號）
}

SOUTHAMPTON_WARNING = (
    "⚠️ Southampton 警告：依 EU Regulation (EC) 1333/2008 Article 24，"
    "含此著色劑的食品須加標示：'may have an adverse effect on activity "
    "and attention in children'（可能影響兒童注意力）"
)

# ── EU 已禁用添加物（靜態清單）─────────────────────────────────────────
# 格式：E-number -> { reason, effective_date, legal_ref }
EU_BANNED_MAP: dict[str, dict] = {
    "E171": {
        "reason": "TiO2 二氧化鈦：EFSA 2021 年重新評估後發現無法排除基因毒性疑慮",
        "effective_date": "2022-08-07",
        "legal_ref": "Regulation (EU) 2022/63",
        "source_url": "https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:32022R0063",
        "note": "自 2022-08-07 起，禁止在歐盟境內作為食品添加物使用",
    },
    # 若未來有新增禁用物質，在此新增
}

# QS 適量原則的標示文字
QS_NOTE = (
    "QS 原則（quantum satis）：無最大限量規定，依「技術需要最小量」使用，"
    "GMP 規範下適量添加"
)

# Carry-over 原則說明
CARRY_OVER_NOTE = (
    "Carry-over 帶入原則（Annex III, Part 5）：若食品成分本身含有此添加物，"
    "允許微量帶入終端食品，但不得超過成分本身允許量"
)


# ═══════════════════════════════════════════════════════════════════════════
# EFSA 爬蟲主類別
# ═══════════════════════════════════════════════════════════════════════════

class EfsaScraper(BaseScraper):
    """
    E-Number 驅動的 EU EFSA / 歐盟委員會食品添加物資料庫爬蟲。

    查詢策略：
      1. 靜態禁用清單（EU_BANNED_MAP）→ 直接標記禁用
      2. Southampton 六種偶氮染料 → 加入警告標示
      3. EU 食品添加物資料庫線上查詢
      4. 若無 E-Number → needs_manual_review
    """

    SCRAPER_NAME = "EfsaScraper"
    COUNTRY_CODE = COUNTRY_CODE

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    # ── 主抓取方法 ────────────────────────────────────────────────────────

    async def scrape_one(self, additive: dict, page: Page) -> Optional[dict]:
        """
        抓取單一 additive 的 EU EFSA 法規狀態。

        Args:
            additive: db_compact.json 的單一條目
            page:     Playwright Page

        Returns:
            patch dict 或 None
        """
        additive_id: str = additive.get("id", "")
        e_number: str = (additive.get("eNumber") or "").strip().upper()
        cas: str = (additive.get("casNumber") or "").strip()
        name_en: str = (additive.get("nameEN") or "").strip()

        self._log.info("scrape_one_start", id=additive_id, e_number=e_number, name=name_en)

        # ── 步驟 1：靜態禁用清單 ─────────────────────────────────────
        if e_number and e_number in EU_BANNED_MAP:
            return self._build_banned_patch(additive_id, e_number)

        # ── 步驟 2：無 E-Number → 標記需人工審核 ──────────────────────
        if not e_number:
            reason = (
                f"無 E-Number，無法在 EU Regulation 1333/2008 中查詢。"
                f"CAS={cas or '無'}, nameEN={name_en or '無'}"
            )
            self._log.warning("no_enumber", id=additive_id, reason=reason)
            return {
                "id": additive_id,
                "regulations": {
                    COUNTRY_CODE: {
                        "permitted": None,
                        **self.provenance_review(EU_DB_BASE, reason),
                    }
                },
            }

        # ── 步驟 3：Southampton 偶氮染料警告（獨立於核准狀態之外）────
        southampton_warning: Optional[str] = (
            SOUTHAMPTON_WARNING if e_number in SOUTHAMPTON_SIX else None
        )

        # ── 步驟 4：EU 食品添加物資料庫線上查詢 ─────────────────────
        result = await self._query_eu_db(page, additive_id, e_number)
        if result:
            # 注入 Southampton 警告
            reg = result["regulations"][COUNTRY_CODE]
            if southampton_warning:
                existing_notes = reg.get("notes", "")
                reg["southamptonWarning"] = True
                reg["notes"] = (
                    f"{existing_notes}\n{southampton_warning}".strip()
                    if existing_notes
                    else southampton_warning
                )
            return result

        # ── 步驟 5：EU DB 查詢失敗 → 嘗試 EFSA 搜尋 ──────────────────
        result = await self._query_efsa_search(page, additive_id, e_number)
        if result:
            reg = result["regulations"][COUNTRY_CODE]
            if southampton_warning:
                reg["southamptonWarning"] = True
                reg["notes"] = (
                    f"{reg.get('notes', '')}\n{southampton_warning}".strip()
                )
            return result

        # ── 步驟 6：全部失敗 ──────────────────────────────────────────
        reason = f"無法在 EU 資料庫查到 {e_number}（{name_en}），請人工確認"
        reg_data: dict = {
            "permitted": None,
            **self.provenance_review(EU_DB_BASE, reason),
        }
        if southampton_warning:
            reg_data["southamptonWarning"] = True
            reg_data["notes"] = southampton_warning
        return {"id": additive_id, "regulations": {COUNTRY_CODE: reg_data}}

    # ── 靜態禁用 patch ────────────────────────────────────────────────────

    def _build_banned_patch(self, additive_id: str, e_number: str) -> dict:
        """從 EU_BANNED_MAP 建立禁用 patch。"""
        info = EU_BANNED_MAP[e_number]
        return {
            "id": additive_id,
            "regulations": {
                COUNTRY_CODE: {
                    "permitted": False,
                    "status": "BANNED",
                    "bannedDate": info.get("effective_date"),
                    "legalRef": info.get("legal_ref"),
                    "notes": info.get("note", "歐盟已禁止此添加物"),
                    **make_provenance(
                        source_url=info.get("source_url", EURLEX_REG_URL),
                        needs_review=False,
                    ),
                }
            },
        }

    # ── EU 食品添加物資料庫查詢 ───────────────────────────────────────────

    async def _query_eu_db(
        self, page: Page, additive_id: str, e_number: str
    ) -> Optional[dict]:
        """
        查詢歐盟委員會食品添加物資料庫（food.ec.europa.eu）。

        策略：EU 資料庫提供可搜尋的 JSON API，
        API 端點：https://ec.europa.eu/food/food_additives/{E-number}
        """
        # 嘗試 EU 食品安全 API（非官方但穩定）
        # 此端點回傳 JSON，包含允許食品類別及最大限量
        api_url = (
            f"https://food.ec.europa.eu/food-safety/food-improvement-agents/"
            f"food-additives/eu-approved-additives-and-e-numbers_en"
        )

        # 直接導覽到含 E-number 錨點的頁面
        target_url = f"{EU_DB_BASE}?search={e_number}"
        self._log.info("eu_db_query", id=additive_id, e_number=e_number, url=target_url)

        success = await self._fetch_with_retry(
            page, target_url,
            wait_selector=".table, table, #additives-table, .additives-list"
        )
        if not success:
            return None

        await self.human_pause(1.0, 2.5)
        final_url = page.url

        # 嘗試從頁面中找到 E-number 對應的表格列
        page_text = await page.inner_text("body")
        e_num_clean = e_number.replace("E", "").lstrip("0")  # "E102" -> "102"

        # 尋找含此 E-number 的段落
        lines = page_text.split("\n")
        relevant_lines = [
            l for l in lines
            if e_number.lower() in l.lower() or f"e{e_num_clean}" in l.lower()
        ]

        if not relevant_lines:
            self._log.info("eu_db_not_found", id=additive_id, e_number=e_number)
            return None

        # 解析允許狀態與最大限量
        combined_text = " ".join(relevant_lines).lower()
        permitted = self._parse_eu_permitted(combined_text)
        max_level, is_qs = self._parse_eu_max_level(combined_text)
        notes = QS_NOTE if is_qs else None

        reg_data: dict = {
            "permitted": permitted,
            "maxLevel": max_level,
            "annex": "Annex II",
            "legalRef": "Regulation (EC) No 1333/2008",
        }
        if notes:
            reg_data["notes"] = notes
        reg_data.update(make_provenance(source_url=final_url, needs_review=False))

        return {"id": additive_id, "regulations": {COUNTRY_CODE: reg_data}}

    def _parse_eu_permitted(self, text: str) -> bool:
        """從文字判斷 EU 是否核准。"""
        if any(k in text for k in ["prohibited", "not permitted", "banned", "withdrawn"]):
            return False
        if any(k in text for k in ["permitted", "authorised", "authorized", "approved", "quantum satis", "mg/kg", "g/kg"]):
            return True
        return True  # EU DB 中出現即預設核准

    def _parse_eu_max_level(self, text: str) -> tuple[Optional[str], bool]:
        """
        解析最大限量。
        回傳 (max_level_str, is_quantum_satis)
        """
        # 偵測 quantum satis（QS）
        if any(k in text for k in ["quantum satis", " qs", "(qs)"]):
            return "quantum satis (QS)", True

        # 數值限量
        # 格式：123 mg/kg, 1.5 g/kg, 50 mg/l 等
        pattern = r"(\d+(?:\.\d+)?)\s*(mg/kg|g/kg|mg/l|mg/litre|%)"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return f"{match.group(1)} {match.group(2).lower()}", False

        return None, False

    # ── EFSA 文獻搜尋（備用）─────────────────────────────────────────────

    async def _query_efsa_search(
        self,
        page: Page,
        additive_id: str,
        e_number: str,
    ) -> Optional[dict]:
        """
        在 EFSA 線上圖書館搜尋評估報告，作為備用資料來源。
        主要用途：確認最新安全評估結論（特別是重新評估計畫下的物質）。
        """
        url = EFSA_SEARCH_URL.format(enumber=e_number)
        self._log.info("efsa_search", id=additive_id, e_number=e_number, url=url)

        success = await self._fetch_with_retry(page, url, wait_selector=".search-results, .publications-list, article")
        if not success:
            return None

        await self.human_pause(1.5, 3.0)
        final_url = page.url

        # 找到評估報告的標題
        titles = await page.query_selector_all(".publication-title, h3, .item-title")
        if not titles:
            return None

        first_title_text = await titles[0].inner_text() if titles else ""
        result_url = final_url

        # 嘗試取得第一筆結果的連結
        first_link = await page.query_selector(".publication-title a, h3 a, .item-title a")
        if first_link:
            href = await first_link.get_attribute("href")
            if href and href.startswith("http"):
                result_url = href
            elif href:
                result_url = f"https://efsa.onlinelibrary.wiley.com{href}"

        # EFSA 有評估報告表示此物質已受 EU 監管（大多數是核准的）
        # 但需人工確認最終結論
        has_re_evaluation = any(
            k in first_title_text.lower()
            for k in ["re-evaluation", "safety assessment", "scientific opinion"]
        )

        review_needed = has_re_evaluation  # 重新評估中的物質需人工確認最終結論

        reg_data: dict = {
            "permitted": None if review_needed else True,
            "annex": "Annex II",
            "legalRef": "Regulation (EC) No 1333/2008",
            "efsa_opinion": first_title_text[:200] if first_title_text else None,
        }

        if review_needed:
            reg_data.update(
                self.provenance_review(
                    url=result_url,
                    reason=f"EFSA 正在進行重新評估（{first_title_text[:100]}），建議確認最終結論",
                )
            )
        else:
            reg_data.update(make_provenance(source_url=result_url, needs_review=False))

        return {"id": additive_id, "regulations": {COUNTRY_CODE: reg_data}}


# ═══════════════════════════════════════════════════════════════════════════
# 獨立執行（測試用）
# ═══════════════════════════════════════════════════════════════════════════

async def _demo():
    """示範：抓取幾種添加物的 EU 狀態"""
    test_additives = [
        {"id": "INS-102", "casNumber": "1934-21-0",  "eNumber": "E102", "nameEN": "Tartrazine",       "functionalClass": "著色劑"},
        {"id": "INS-171", "casNumber": "13463-67-7", "eNumber": "E171", "nameEN": "Titanium Dioxide",  "functionalClass": "著色劑"},
        {"id": "INS-124", "casNumber": "2611-82-7",  "eNumber": "E124", "nameEN": "Ponceau 4R",        "functionalClass": "著色劑"},
        {"id": "INS-200", "casNumber": "110-44-1",   "eNumber": "E200", "nameEN": "Sorbic Acid",       "functionalClass": "防腐劑"},
        {"id": "INS-211", "casNumber": "532-32-1",   "eNumber": "E211", "nameEN": "Sodium Benzoate",   "functionalClass": "防腐劑"},
        {"id": "TW-NO-E", "casNumber": "12345-00-0", "eNumber": "",     "nameEN": "No E-Number Test",  "functionalClass": "其他"},
    ]

    scraper = EfsaScraper(headless=True, concurrency=2)
    patches = await scraper.run(test_additives)

    print("\n===== EU EFSA 爬蟲結果 =====")
    for p in patches:
        eu = (p.get("regulations") or {}).get("EU", {})
        review = eu.get("needs_manual_review", False)
        permitted = eu.get("permitted")
        southampton = "🔴 Southampton" if eu.get("southamptonWarning") else ""
        banned_date = eu.get("bannedDate", "")
        print(
            f"[{'⚠️  審核' if review else '✅ OK   '}] "
            f"{p['id']:12s} | permitted={str(permitted):5s} | "
            f"maxLevel={str(eu.get('maxLevel', '—')):20s} | "
            f"{southampton} {f'banned={banned_date}' if banned_date else ''} | "
            f"url={eu.get('source_url', '—')[:50]}"
        )


if __name__ == "__main__":
    asyncio.run(_demo())
