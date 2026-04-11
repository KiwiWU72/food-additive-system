"""
base_scraper.py — 食品添加物合規查詢系統：爬蟲基礎類別
==========================================================
提供所有子爬蟲共用的：
  - Playwright 瀏覽器管理（帶反偵測 headers）
  - 指數退避重試邏輯（最多 MAX_RETRIES 次）
  - 來源溯源欄位：source_url, last_scraped_at, needs_manual_review
  - 結構化日誌（structlog）
  - 統一的輸出 patch 格式，可直接 merge 回 db_compact.json

使用方式：
    from base_scraper import BaseScraper
    class MyScraper(BaseScraper):
        async def scrape_one(self, additive: dict) -> dict:
            ...

每月更新執行方式：
    python run_update.py --input db_compact.json --output patch.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ── 可選：structlog（pip install structlog）────────────────────────────────
try:
    import structlog
    logger = structlog.get_logger(__name__)
    USE_STRUCTLOG = True
except ImportError:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    )
    logger = logging.getLogger(__name__)
    USE_STRUCTLOG = False

# ── Playwright（pip install playwright && playwright install chromium）─────
try:
    from playwright.async_api import (
        async_playwright,
        Browser,
        BrowserContext,
        Page,
        TimeoutError as PWTimeoutError,
    )
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False
    raise RuntimeError(
        "Playwright 未安裝。請執行：\n"
        "  pip install playwright\n"
        "  playwright install chromium"
    )

# ═══════════════════════════════════════════════════════════════════════════
# 常數
# ═══════════════════════════════════════════════════════════════════════════

MAX_RETRIES: int = 3          # 最大重試次數
BASE_DELAY: float = 2.0       # 基礎等待秒數（指數退避）
JITTER_MAX: float = 1.5       # 加入亂數抖動（秒），避免被偵測為機器人
PAGE_TIMEOUT: int = 30_000    # 頁面載入逾時（毫秒）
NAV_TIMEOUT: int = 60_000     # 導覽逾時（毫秒）

# 模擬真實瀏覽器 User-Agent（定期更新此清單）
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# 回傳欄位名稱常數，與 db_compact.json schema 保持一致
FIELD_SOURCE_URL = "source_url"
FIELD_LAST_SCRAPED = "last_scraped_at"
FIELD_NEEDS_REVIEW = "needs_manual_review"
FIELD_REVIEW_REASON = "review_reason"


# ═══════════════════════════════════════════════════════════════════════════
# 工具函式
# ═══════════════════════════════════════════════════════════════════════════

def now_utc() -> str:
    """回傳目前 UTC 時間的 ISO-8601 字串，例：2025-06-15T08:30:00Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_provenance(
    source_url: str,
    needs_review: bool = False,
    review_reason: Optional[str] = None,
) -> dict[str, Any]:
    """
    建立標準溯源欄位字典。
    嵌入到 additive 的每個 regulation 子物件中。

    範例輸出：
    {
        "source_url": "https://www.ecfr.gov/...",
        "last_scraped_at": "2025-06-15T08:30:00Z",
        "needs_manual_review": false,
        "review_reason": null
    }
    """
    p: dict[str, Any] = {
        FIELD_SOURCE_URL: source_url,
        FIELD_LAST_SCRAPED: now_utc(),
        FIELD_NEEDS_REVIEW: needs_review,
    }
    if needs_review and review_reason:
        p[FIELD_REVIEW_REASON] = review_reason
    return p


def flag_for_review(
    source_url: str,
    reason: str,
) -> dict[str, Any]:
    """
    標記需要人工審核的溯源資訊（抓取失敗或資料不確定時使用）。
    """
    return make_provenance(source_url, needs_review=True, review_reason=reason)


def exponential_backoff(attempt: int) -> float:
    """
    計算第 attempt 次重試的等待秒數。
    公式：BASE_DELAY × 2^attempt + random(0, JITTER_MAX)
    例：第0次=2s, 第1次=4s, 第2次=8s（各加最多1.5s的抖動）
    """
    delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, JITTER_MAX)
    return delay


# ═══════════════════════════════════════════════════════════════════════════
# 基礎爬蟲類別
# ═══════════════════════════════════════════════════════════════════════════

class BaseScraper(ABC):
    """
    所有食品添加物法規爬蟲的基礎類別。

    子類別必須實作：
        async def scrape_one(self, additive: dict) -> dict

    scrape_one 應回傳一個 patch dict，格式如下：
    {
        "id": "INS-200",               # additive 的唯一 ID
        "regulations": {
            "US": {                     # 國家代碼
                "permitted": true,
                "maxLevel": "0.1%",
                "notes": "...",
                "source_url": "https://...",
                "last_scraped_at": "2025-06-15T08:30:00Z",
                "needs_manual_review": false
            }
        }
    }
    若無法取得資料，回傳 needs_manual_review: true 並說明原因。
    """

    # 子類別應覆寫這個屬性
    SCRAPER_NAME: str = "BaseScraper"

    def __init__(
        self,
        headless: bool = True,
        slow_mo: int = 0,
        concurrency: int = 3,
    ) -> None:
        """
        Args:
            headless:    True = 無頭模式（正式執行）；False = 顯示瀏覽器（除錯）
            slow_mo:     每個操作間的延遲毫秒數（除錯用）
            concurrency: 同時執行的分頁數量（避免速率限制，建議 ≤ 3）
        """
        self.headless = headless
        self.slow_mo = slow_mo
        self.concurrency = concurrency
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

        self._log = logger.bind(scraper=self.SCRAPER_NAME) if USE_STRUCTLOG else logger

    # ── 瀏覽器生命週期 ─────────────────────────────────────────────────────

    async def _start_browser(self, playwright_instance) -> None:
        """啟動 Chromium 並建立帶反偵測設定的 BrowserContext。"""
        self._browser = await playwright_instance.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",  # 隱藏自動化特徵
                "--disable-infobars",
                "--window-size=1920,1080",
            ],
        )
        ua = random.choice(USER_AGENTS)
        self._context = await self._browser.new_context(
            user_agent=ua,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
            timezone_id="America/New_York",
            # 偽裝成真實瀏覽器的額外 HTTP headers
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "DNT": "1",
            },
        )
        # 注入 JS：移除 navigator.webdriver 屬性（常被偵測）
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self._log.info("browser_started", user_agent=ua, headless=self.headless)

    async def _stop_browser(self) -> None:
        """關閉瀏覽器並釋放資源。"""
        if self._browser:
            await self._browser.close()
            self._browser = None
            self._context = None

    async def _new_page(self) -> Page:
        """建立新分頁。在 _start_browser 之後呼叫。"""
        assert self._context is not None, "必須先呼叫 _start_browser()"
        page = await self._context.new_page()
        page.set_default_timeout(PAGE_TIMEOUT)
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        return page

    # ── 重試邏輯 ───────────────────────────────────────────────────────────

    async def _fetch_with_retry(
        self,
        page: Page,
        url: str,
        wait_selector: Optional[str] = None,
    ) -> bool:
        """
        導覽到 URL，失敗時以指數退避重試。

        Args:
            page:           Playwright Page 物件
            url:            要載入的 URL
            wait_selector:  等待此 CSS selector 出現才算成功（可選）

        Returns:
            True = 成功；False = 超過重試次數仍失敗
        """
        for attempt in range(MAX_RETRIES):
            try:
                self._log.info("navigate", url=url, attempt=attempt + 1)
                await page.goto(url, wait_until="domcontentloaded")
                if wait_selector:
                    await page.wait_for_selector(wait_selector, timeout=PAGE_TIMEOUT)
                return True

            except PWTimeoutError as e:
                self._log.warning(
                    "timeout",
                    url=url,
                    attempt=attempt + 1,
                    error=str(e)[:120],
                )
            except Exception as e:  # noqa: BLE001
                self._log.warning(
                    "nav_error",
                    url=url,
                    attempt=attempt + 1,
                    error=str(e)[:120],
                )

            if attempt < MAX_RETRIES - 1:
                wait = exponential_backoff(attempt)
                self._log.info("retry_wait", seconds=round(wait, 2))
                await asyncio.sleep(wait)

        self._log.error("max_retries_exceeded", url=url)
        return False

    # ── 並行抓取控制 ──────────────────────────────────────────────────────

    async def _scrape_batch(
        self,
        additives: list[dict],
        semaphore: asyncio.Semaphore,
    ) -> list[dict]:
        """以 semaphore 控制並行數量，逐一抓取 additives 列表。"""
        tasks = [self._scrape_with_semaphore(a, semaphore) for a in additives]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        patches: list[dict] = []
        for additive, result in zip(additives, results):
            if isinstance(result, Exception):
                self._log.error(
                    "scrape_exception",
                    id=additive.get("id"),
                    error=str(result)[:200],
                )
                # 例外視為需要人工審核
                patches.append({
                    "id": additive["id"],
                    "regulations": {
                        self.COUNTRY_CODE: flag_for_review(  # type: ignore[attr-defined]
                            source_url="",
                            reason=f"Unhandled exception: {str(result)[:200]}",
                        )
                    },
                })
            elif result:
                patches.append(result)
        return patches

    async def _scrape_with_semaphore(
        self,
        additive: dict,
        semaphore: asyncio.Semaphore,
    ) -> Optional[dict]:
        """取得 semaphore 後才執行 scrape_one，確保並行上限。"""
        async with semaphore:
            page = await self._new_page()
            try:
                return await self.scrape_one(additive, page)
            finally:
                await page.close()

    # ── 主要公開介面 ──────────────────────────────────────────────────────

    async def run(self, additives: list[dict]) -> list[dict]:
        """
        對整個 additives 清單執行爬取，回傳 patch 清單。

        Args:
            additives: db_compact.json 中的 additive 物件列表

        Returns:
            patch 清單，格式同 scrape_one 的回傳值
        """
        semaphore = asyncio.Semaphore(self.concurrency)
        async with async_playwright() as pw:
            await self._start_browser(pw)
            self._log.info(
                "run_start",
                total=len(additives),
                concurrency=self.concurrency,
            )
            try:
                patches = await self._scrape_batch(additives, semaphore)
            finally:
                await self._stop_browser()

        ok = sum(
            1 for p in patches
            if not any(
                v.get(FIELD_NEEDS_REVIEW)
                for v in (p.get("regulations") or {}).values()
                if isinstance(v, dict)
            )
        )
        self._log.info(
            "run_complete",
            total=len(additives),
            patched=len(patches),
            ok=ok,
            needs_review=len(patches) - ok,
        )
        return patches

    # ── 子類別必須實作 ────────────────────────────────────────────────────

    @abstractmethod
    async def scrape_one(self, additive: dict, page: Page) -> Optional[dict]:
        """
        抓取單一 additive 的法規資訊。

        Args:
            additive:  db_compact.json 中的單一 additive 物件
            page:      已開啟的 Playwright Page（每次呼叫獨立）

        Returns:
            patch dict（見類別說明），或 None（跳過此項目）
        """
        ...

    # ── 工具方法（供子類別使用）──────────────────────────────────────────

    def provenance_ok(self, url: str) -> dict:
        """建立成功溯源資訊的捷徑方法。"""
        return make_provenance(url, needs_review=False)

    def provenance_review(self, url: str, reason: str) -> dict:
        """建立需人工審核溯源資訊的捷徑方法。"""
        return flag_for_review(url, reason)

    async def human_pause(self, min_s: float = 0.5, max_s: float = 2.0) -> None:
        """隨機等待，模擬人類操作間隔，降低被封鎖機率。"""
        await asyncio.sleep(random.uniform(min_s, max_s))
