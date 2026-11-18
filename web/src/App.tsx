import { useState } from "react";
import { detectBrowserLocale, type Locale, type MessageKey, WEB_MESSAGES } from "./i18n";

type TabId = "manuscript" | "appendices" | "reviews" | "figures";

export function App() {
  const [locale, setLocale] = useState<Locale>(detectBrowserLocale);
  const [activeTab, setActiveTab] = useState<TabId>("manuscript");

  const t = (key: MessageKey): string => WEB_MESSAGES[locale][key];
  const tabs: readonly { id: TabId; labelKey: MessageKey }[] = [
    { id: "manuscript", labelKey: "tab.manuscript" },
    { id: "appendices", labelKey: "tab.appendices" },
    { id: "reviews", labelKey: "tab.reviews" },
    { id: "figures", labelKey: "tab.figures" },
  ];

  return (
    <div style={{ fontFamily: "system-ui, -apple-system, sans-serif", margin: 0, color: "#18181b" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "12px 20px",
          borderBottom: "1px solid #e4e4e7",
        }}
      >
        <div>
          <strong>{t("app.title")}</strong>
          <span style={{ marginLeft: 12, color: "#52525b", fontSize: 14 }}>{t("app.subtitle")}</span>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <label style={{ fontSize: 13, color: "#52525b" }}>
            {t("diff.label")}:{" "}
            <select defaultValue="last_checkpoint">
              <option value="last_checkpoint">{t("diff.last_checkpoint")}</option>
              <option value="resolution_snapshot">{t("diff.resolution_snapshot")}</option>
              <option value="submission_milestone">{t("diff.submission_milestone")}</option>
            </select>
          </label>
          <button
            type="button"
            onClick={() => setLocale(locale === "en" ? "zh-CN" : "en")}
            style={{ padding: "4px 10px", cursor: "pointer" }}
          >
            {t("app.lang_toggle")}
          </button>
        </div>
      </header>
      <nav style={{ display: "flex", gap: 4, padding: "8px 20px", borderBottom: "1px solid #f4f4f5" }}>
        {tabs.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => setActiveTab(tab.id)}
            style={{
              padding: "6px 12px",
              border: "none",
              borderBottom: activeTab === tab.id ? "2px solid #18181b" : "2px solid transparent",
              background: "transparent",
              fontWeight: activeTab === tab.id ? 600 : 400,
              cursor: "pointer",
            }}
          >
            {t(tab.labelKey)}
          </button>
        ))}
      </nav>
      <main style={{ padding: "32px 20px", color: "#52525b" }}>{t("empty.workspace")}</main>
    </div>
  );
}
