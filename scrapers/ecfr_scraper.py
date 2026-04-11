"""
ecfr_scraper.py — FDA eCFR 食品添加物法規爬蟲（以 CAS Number 為索引鍵）
=======================================================================
目標網站：
  - eCFR Title 21（食品法規）：https://www.ecfr.gov/current/title-21
  - FDA EVERYTHING ADDED TO FOOD (EAFUS) 資料庫：
    https://www.cfsanappsexternal.fda.gov/scripts/fdcc/?set=EAFUS
  - FDA FoodAdditive Status List：
    https://www.fda.gov/food/food-additives-petitions/food-additive-status-list

查詢流程：
  1. 以 CAS Number 搜尋 EAFUS 資料庫
  2. 取得 GRAS/Approved/Prohibited 狀態
  3. 若 additive 屬顏色添加物（FD&C / colorants），
     另查 21 CFR Part 73/74 確認批次認證需求
  4. 寫入 source_url, last_scraped_at, needs_manual_review

每月更新：
  python run_update.py --scraper us --input db_compact.json --output patch_us.json

注意事項：
  - EAFUS 搜尋為 POST request，本腳本使用 Playwright 模擬表單送出
  - eCFR 全文搜尋用 https://www.ecfr.gov/search?query={CAS}&per_page=10
  - 若 CAS Number 為空，嘗試以 nameEN 搜尋；仍失敗則標記 needs_manual_review
  - FDA 批次認證資料來自靜態對照表（BATCH_CERT_MAP），每年約更新一次
"""

from __future__ import annotations

import asyncio
import re
from typing import Optional

from playwright.async_api import Page

from base_scraper import BaseScraper, flag_for_review, make_provenance

# ═══════════════════════════════════════════════════════════════════════════
# 常數
# ═══════════════════════════════════════════════════════════════════════════

COUNTRY_CODE = "US"

# EAFUS 搜尋 API（以 CAS Number 查詢）
EAFUS_SEARCH_URL = (
    "https://www.cfsanappsexternal.fda.gov/scripts/fdcc/"
    "?set=EAFUS&sort=NAME&start=1&type=basic&search={cas}"
)

# eCFR 全文搜尋（備用：當 EAFUS 無結果時）
ECFR_SEARCH_URL = (
    "https://www.ecfr.gov/search"
    "?query={query}&hierarchy%5B%5D=title-21&per_page=5"
)

# FDA 食品添加物狀態頁（總覽）
FDA_STATUS_BASE = "https://www.fda.gov/food/food-additives-petitions/food-additive-status-list"

# eCFR Title 21 Part 182/184 GRAS 清單
ECFR_GRAS_URL = "https://www.ecfr.gov/current/title-21/chapter-I/subchapter-B/part-182"

# ── FD&C 批次認證對照表 ────────────────────────────────────────────────
# 來源：21 CFR Part 73（免認證）& Part 74（需批次認證）
# 格式：CAS Number -> { "fdcName": ..., "partRef": ..., "batchCertRequired": bool }
# 每年約更新一次（新增或撤銷的顏色添加物）
# 最後更新：2025-01-15（FDA 撤銷 FD&C Red No. 3）
BATCH_CERT_MAP: dict[str, dict] = {
    # ── Part 74：需要批次認證（Certification Required）─────────────────
    "2650-18-2":   {"fdcName": "FD&C Blue No. 1",    "partRef": "21 CFR 74.101", "batchCertRequired": True},
    "860-22-0":    {"fdcName": "FD&C Blue No. 2",    "partRef": "21 CFR 74.102", "batchCertRequired": True},
    "4680-78-8":   {"fdcName": "FD&C Green No. 3",   "partRef": "21 CFR 74.203", "batchCertRequired": True},
    "1934-21-0":   {"fdcName": "FD&C Yellow No. 5",  "partRef": "21 CFR 74.705", "batchCertRequired": True},
    "2783-94-0":   {"fdcName": "FD&C Yellow No. 6",  "partRef": "21 CFR 74.706", "batchCertRequired": True},
    "3844-45-9":   {"fdcName": "FD&C Blue No. 1 (Lake)", "partRef": "21 CFR 74.101", "batchCertRequired": True},
    # Red No. 40（最常用紅色，需批次認證）
    "25956-17-6":  {"fdcName": "FD&C Red No. 40",    "partRef": "21 CFR 74.340", "batchCertRequired": True},

    # ── Red No. 3：已撤銷（Revoked 2025-01-15）───────────────────────
    # FDA Final Rule 90 FR 3126；一般食品 2027-01-15，瑪拉斯奇諾櫻桃 2029-01-15
    "16423-68-0":  {
        "fdcName": "FD&C Red No. 3",
        "partRef": "21 CFR 74.303",
        "batchCertRequired": False,
        "status": "REVOKED",
        "revokedDate": "2025-01-15",
        "complianceDate": "2027-01-15",
        "complianceDateAlt": "2029-01-15",
        "revokeNote": "FDA Final Rule 90 FR 3126 — 已正式撤銷 FD&C Red No. 3 授權",
    },

    # ── Part 73：免批次認證（Exempt from Certification）──────────────
    "1390-65-4":   {"fdcName": "Carmine",             "partRef": "21 CFR 73.100", "batchCertRequired": False},
    "7235-40-7":   {"fdcName": "Beta-Carotene",       "partRef": "21 CFR 73.95",  "batchCertRequired": False},
    "120-80-9":    {"fdcName": "Grape skin extract",  "partRef": "21 CFR 73.170", "batchCertRequired": False},
    "8015-67-6":   {"fdcName": "Paprika oleoresin",   "partRef": "21 CFR 73.345", "batchCertRequired": False},
    "465-42-9":    {"fdcName": "Saffron",             "partRef": "21 CFR 73.500", "batchCertRequired": False},
    "20283-92-5":  {"fdcName": "Turmeric",            "partRef": "21 CFR 73.600", "batchCertRequired": False},
    "13463-67-7":  {"fdcName": "Titanium Dioxide",    "partRef": "21 CFR 73.575", "batchCertRequired": False},

    # ── 未在 FDA 核准列表的著色劑（台灣允許但美國不允許）─────────────
    # 查到這些 CAS 時應標記 NOT_APPROVED
    "2611-82-7":   {"fdcName": None, "status": "NOT_APPROVED",
                    "note": "Ponceau 4R (E124) — 美國未核准，僅台灣/EU 允許"},
    "8004-92-4":   {"fdcName": None, "status": "NOT_APPROVED",
                    "note": "Quinoline Yellow (E104) — 美國未核准"},
    "514-78-3":    {"fdcName": None, "status": "NOT_APPROVED",
                    "note": "Canthaxanthin (E161g) — 美國禁止用於食品著色"},
}

# GRAS 狀態關鍵字（EAFUS 回傳文字中可能出現的字串）
GRAS_KEYWORDS = ["gras", "generally recognized as safe", "§182", "§184", "part 182", "part 184"]
APPROVED_KEYWORDS = ["approved", "sanctioned", "permitted", "affirmed"]
PROHIBITED_KEYWORDS = ["prohibited", "banned", "not permitted", "not approved", "revoked"]


# ═══════════════════════════════════════════════════════════════════════════
# eCFR 爬蟲主類別
# ═══════════════════════════════════════════════════════════════════════════

class EcfrScraper(BaseScraper):
    """
    CAS Number 驅動的 FDA eCFR / EAFUS 爬蟲。

    優先順序：
      1. 靜態批次認證對照表（BATCH_CERT_MAP）→ 直接回傳，不需上網
      2. EAFUS 搜尋（以 CAS Number）
      3. eCFR 全文搜尋（以 CAS 或品名）
      4. 若仍無結果 → needs_manual_review = True
    """

    SCRAPER_NAME = "EcfrScraper"
    COUNTRY_CODE = COUNTRY_CODE

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    # ── 主抓取方法 ────────────────────────────────────────────────────────

    async def scrape_one(self, additive: dict, page: Page) -> Optional[dict]:
        """
        抓取單一 additive 的 US FDA 法規狀態。

        Args:
            additive: db_compact.json 的單一條目（需有 id, casNumber, nameEN）
            page:     Playwright Page

        Returns:
            patch dict 或 None（若此 additive 無 CAS Number 且非顏色劑）
        """
        additive_id: str = additive.get("id", "")
        cas: str = (additive.get("casNumber") or "").strip()
        name_en: str = (additive.get("nameEN") or "").strip()
        functional_class: str = (additive.get("functionalClass") or "").strip()

        self._log.info("scrape_one_start", id=additive_id, cas=cas, name=name_en)

        # ── 步驟 1：靜態批次認證對照表優先查詢 ──────────────────────────
        if cas and cas in BATCH_CERT_MAP:
            return self._build_patch_from_map(additive_id, cas)

        # ── 步驟 2：EAFUS 線上查詢 ────────────────────────────────────
        if cas:
            result = await self._query_eafus(page, additive_id, cas)
            if result:
                return result

        # ── 步驟 3：eCFR 全文搜尋（備用）──────────────────────────────
        query = cas if cas else name_en
        if query:
            result = await self._query_ecfr_search(page, additive_id, query, cas)
            if result:
                return result

        # ── 步驟 4：全部失敗 → 標記需人工審核 ────────────────────────
        reason = (
            f"無法在 EAFUS/eCFR 找到資料。CAS={cas or '無'}, "
            f"nameEN={name_en or '無'}, class={functional_class}"
        )
        self._log.warning("needs_review", id=additive_id, reason=reason)
        return {
            "id": additive_id,
            "regulations": {
                COUNTRY_CODE: {
                    "permitted": None,
                    **self.provenance_review(EAFUS_SEARCH_URL.format(cas=cas or ""), reason),
                }
            },
        }

    # ── 靜態對照表查詢 ────────────────────────────────────────────────────

    def _build_patch_from_map(self, additive_id: str, cas: str) -> dict:
        """從 BATCH_CERT_MAP 直接建立 patch（不需網路）。"""
        info = BATCH_CERT_MAP[cas]
        status = info.get("status", "APPROVED")

        # 撤銷中（已無效）
        if status == "REVOKED":
            reg_data = {
                "permitted": False,
                "status": "REVOKED",
                "fdcName": info["fdcName"],
                "partRef": info["partRef"],
                "revokedDate": info.get("revokedDate"),
                "complianceDate": info.get("complianceDate"),
                "complianceDateAlt": info.get("complianceDateAlt"),
                "notes": info.get("revokeNote", ""),
                **make_provenance(
                    source_url=f"https://www.federalregister.gov/documents/2025/01/15/2025-00395/listing-of-color-additives-exempt-from-certification",
                    needs_review=False,
                ),
            }
        # 美國未核准
        elif status == "NOT_APPROVED":
            reg_data = {
                "permitted": False,
                "status": "NOT_APPROVED",
                "notes": info.get("note", "美國未核准此著色劑"),
                **self.provenance_review(
                    url=FDA_STATUS_BASE,
                    reason=info.get("note", "美國未核准"),
                ),
            }
        else:
            # 正常核准（含批次認證資訊）
            reg_data = {
                "permitted": True,
                "fdcName": info["fdcName"],
                "partRef": info["partRef"],
                "fdcCertification": {
                    "required": True,
                    "batchCertRequired": info["batchCertRequired"],
                    "certBody": "FDA（由 FDA 實驗室逐批認證）" if info["batchCertRequired"] else "豁免認證（21 CFR Part 73）",
                },
                **make_provenance(
                    source_url=f"https://www.ecfr.gov/current/title-21/chapter-I/subchapter-A/{info['partRef'].replace('21 CFR ', '').split('.')[0].lower()}",
                    needs_review=False,
                ),
            }

        return {"id": additive_id, "regulations": {COUNTRY_CODE: reg_data}}

    # ── EAFUS 線上查詢 ────────────────────────────────────────────────────

    async def _query_eafus(
        self, page: Page, additive_id: str, cas: str
    ) -> Optional[dict]:
        """
        查詢 FDA EAFUS（Everything Added to Food in the United States）資料庫。
        回傳 patch dict 或 None（查無結果）。
        """
        url = EAFUS_SEARCH_URL.format(cas=cas)
        self._log.info("eafus_query", id=additive_id, url=url)

        success = await self._fetch_with_retry(page, url, wait_selector="table, .search-results, #results")
        if not success:
            self._log.warning("eafus_fetch_failed", id=additive_id, cas=cas)
            return None

        await self.human_pause(0.5, 1.5)

        # 取得頁面文字（EAFUS 是純 HTML 表格）
        page_text = (await page.inner_text("body")).lower()
        final_url = page.url

        # 判斷「無結果」
        no_result_signals = ["no records found", "no results", "0 records", "no match"]
        if any(s in page_text for s in no_result_signals):
            self._log.info("eafus_no_result", id=additive_id, cas=cas)
            return None

        # 解析狀態
        permitted, status_note = self._parse_eafus_status(page_text)
        if permitted is None:
            self._log.info("eafus_parse_failed", id=additive_id, cas=cas)
            return None

        # 嘗試取得 CFR 引用（例：21 CFR 182.xxx）
        cfr_ref = self._extract_cfr_ref(page_text)

        reg_data: dict = {
            "permitted": permitted,
            "cfrRef": cfr_ref,
            "notes": status_note,
            **make_provenance(source_url=final_url, needs_review=False),
        }

        return {"id": additive_id, "regulations": {COUNTRY_CODE: reg_data}}

    def _parse_eafus_status(self, page_text: str) -> tuple[Optional[bool], str]:
        """
        解析 EAFUS 頁面文字，判斷核准狀態。
        回傳 (permitted: bool|None, status_note: str)
        """
        text = page_text.lower()

        if any(k in text for k in PROHIBITED_KEYWORDS):
            return False, "FDA EAFUS：Prohibited / Not Approved"
        if any(k in text for k in GRAS_KEYWORDS):
            return True, "GRAS（Generally Recognized as Safe）"
        if any(k in text for k in APPROVED_KEYWORDS):
            return True, "FDA Approved Food Additive"
        return None, ""

    def _extract_cfr_ref(self, page_text: str) -> Optional[str]:
        """從頁面文字中提取 CFR 引用，例：21 CFR 182.1625"""
        pattern = r"21\s+cfr\s+[\d.]+\b"
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            return match.group(0).upper().replace("  ", " ")
        return None

    # ── eCFR 全文搜尋（備用）──────────────────────────────────────────────

    async def _query_ecfr_search(
        self,
        page: Page,
        additive_id: str,
        query: str,
        cas: str,
    ) -> Optional[dict]:
        """
        使用 eCFR 全文搜尋 API 查詢。
        當 EAFUS 無結果時使用（例：較舊的 GRAS 物質）。
        """
        url = ECFR_SEARCH_URL.format(query=query.replace(" ", "+"))
        self._log.info("ecfr_search", id=additive_id, query=query, url=url)

        # eCFR 搜尋結果為 JSON API
        # 嘗試直接呼叫 API 端點（比解析 HTML 更穩定）
        api_url = (
            f"https://www.ecfr.gov/api/search/v1/results"
            f"?query={query.replace(' ', '+')}"
            f"&hierarchy[]=title-21"
            f"&per_page=5"
        )

        # 使用 Playwright 請求攔截取得 API 回應
        response_data: Optional[dict] = None
        try:
            async with page.expect_response(
                lambda r: "ecfr.gov/api/search" in r.url,
                timeout=PAGE_TIMEOUT,
            ) as resp_info:
                await page.goto(url, wait_until="domcontentloaded")
            response = await resp_info.value
            if response.ok:
                response_data = await response.json()
        except Exception as e:  # noqa: BLE001
            self._log.warning("ecfr_api_intercept_failed", error=str(e)[:100])
            # fallback：直接 fetch API URL
            success = await self._fetch_with_retry(page, api_url)
            if success:
                try:
                    raw = await page.inner_text("pre, body")
                    response_data = __import__("json").loads(raw)
                except Exception:
                    pass

        if not response_data:
            return None

        # 解析搜尋結果
        results = response_data.get("results", [])
        if not results:
            self._log.info("ecfr_no_results", id=additive_id, query=query)
            return None

        # 取第一筆最相關的結果
        first = results[0]
        ecfr_url = f"https://www.ecfr.gov{first.get('url', '')}"
        headings = first.get("headings", {})
        cfr_ref = (
            f"21 CFR {headings.get('part', '')}."
            f"{headings.get('section', '')}".rstrip(".")
        )

        # 判斷狀態（從摘要文字中判斷）
        snippet = (first.get("full_text_excerpt") or "").lower()
        permitted, status_note = self._parse_eafus_status(snippet)

        if permitted is None:
            # 有搜尋結果但無法判斷狀態 → needs_manual_review
            return {
                "id": additive_id,
                "regulations": {
                    COUNTRY_CODE: {
                        "permitted": None,
                        "cfrRef": cfr_ref if cfr_ref != "21 CFR ." else None,
                        **self.provenance_review(
                            url=ecfr_url,
                            reason="eCFR 有搜尋結果但無法自動解析核准狀態，請人工確認",
                        ),
                    }
                },
            }

        return {
            "id": additive_id,
            "regulations": {
                COUNTRY_CODE: {
                    "permitted": permitted,
                    "cfrRef": cfr_ref if cfr_ref != "21 CFR ." else None,
                    "notes": status_note,
                    **make_provenance(source_url=ecfr_url, needs_review=False),
                }
            },
        }


# ═══════════════════════════════════════════════════════════════════════════
# 獨立執行（測試用）
# ═══════════════════════════════════════════════════════════════════════════

async def _demo():
    """示範：抓取 Potassium Sorbate 和 FD&C Red No. 40 的 US 狀態"""
    test_additives = [
        {"id": "INS-202", "casNumber": "24634-61-5", "nameEN": "Potassium Sorbate",   "functionalClass": "防腐劑"},
        {"id": "INS-129", "casNumber": "25956-17-6", "nameEN": "Allura Red AC",       "functionalClass": "著色劑"},
        {"id": "TW-496",  "casNumber": "16423-68-0", "nameEN": "Erythrosine",         "functionalClass": "著色劑"},
        {"id": "INS-407", "casNumber": "9000-07-1",  "nameEN": "Carrageenan",         "functionalClass": "增稠劑"},
        {"id": "UNKNOWN", "casNumber": "",           "nameEN": "Unknown Substance",   "functionalClass": "其他"},
    ]

    scraper = EcfrScraper(headless=True, concurrency=2)
    patches = await scraper.run(test_additives)

    print("\n===== US FDA 爬蟲結果 =====")
    for p in patches:
        us = (p.get("regulations") or {}).get("US", {})
        review = us.get("needs_manual_review", False)
        permitted = us.get("permitted")
        print(
            f"[{'⚠️  審核' if review else '✅ OK   '}] "
            f"{p['id']:12s} | permitted={str(permitted):5s} | "
            f"ref={us.get('cfrRef') or us.get('partRef') or '—':25s} | "
            f"url={us.get('source_url', '—')[:60]}"
        )


if __name__ == "__main__":
    asyncio.run(_demo())
