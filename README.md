# food-additive-system
Automated AI-driven dashboard for Taiwan Food R&amp;D. Maps TW additives to US (FDA/California), EU (EFSA), and Japan (MHLW) regulations using CAS/E-Numbers. Features Playwright-powered monthly auto-updates via GitHub Actions, dynamic phase-out timelines, and deep-tier risk alerts (USDA, Carry-over, Prop 65). High-precision export compliance tool.

# 全球食品添加物合規查詢系統 (Global Food Additive Compliance Dashboard)

### 📖 專案簡介 (Overview)
本系統專為台灣食品研發人員設計，旨在建立一個自動化的國際法規監測平台。透過 CAS Number 與 E-Number，將台灣正面表列之添加物與 **美國 (FDA)**、**歐盟 (EFSA)** 及 **日本 (MHLW)** 的最新法規進行精準對照。

### ✨ 核心功能 (Key Features)
* **多國法規自動映射**：自動比對美、歐、日限量標準，避免人工檢索誤差。
* **深層風險預警**：針對美國 USDA 管轄權、加州 Prop 65、歐盟南安普敦色素警語及帶入原則進行提醒。
* **法規退場時間軸**：追蹤禁用預告期，預警配方修正時間。
* **自動化資料更新**：利用 GitHub Actions 每月自動執行爬蟲，確保法規數據為最新版本。

### 🛠️ 技術架構 (Architecture)
* **前端**：React.js 儀表板
* **資料庫**：動態 JSON 格式
* **自動化**：Python (Playwright) 爬蟲 + GitHub Actions 排程

---

### 🎯 使用對象 (Target Audience)
* **食品研發人員 (R&D)**：新產品開發階段的合規性評估。
* **法規事務人員 (RA)**：外銷合規性複核與全球法規趨勢監控。
