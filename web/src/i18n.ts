export type Locale = "en" | "zh-CN";

export const WEB_MESSAGES = {
  en: {
    "app.title": "oh-my-manuscript",
    "app.subtitle": "Author-first scientific manuscript workbench",
    "app.lang_toggle": "中文",
    "tab.manuscript": "Manuscript",
    "tab.appendices": "Appendices",
    "tab.reviews": "Review Items",
    "tab.figures": "Figures",
    "diff.label": "Diff Baseline",
    "diff.last_checkpoint": "Last Checkpoint",
    "diff.resolution_snapshot": "Resolution Snapshot",
    "diff.submission_milestone": "Submission Milestone",
    "empty.workspace": "Connect a Manuscript Workspace via `omm serve` to start outlining and reviewing.",
  },
  "zh-CN": {
    "app.title": "oh-my-manuscript",
    "app.subtitle": "以作者为第一责任人的科学论文工作台",
    "app.lang_toggle": "EN",
    "tab.manuscript": "正文",
    "tab.appendices": "附录",
    "tab.reviews": "审稿追踪项",
    "tab.figures": "图表总览",
    "diff.label": "对比基准",
    "diff.last_checkpoint": "上一审阅点",
    "diff.resolution_snapshot": "单条批注快照",
    "diff.submission_milestone": "投稿里程碑",
    "empty.workspace": "通过 `omm serve` 连接手稿工作区后即可开始大纲创制与审阅批注。",
  },
} as const;

export type MessageKey = keyof (typeof WEB_MESSAGES)["en"];

export function detectBrowserLocale(): Locale {
  if (typeof navigator !== "undefined" && navigator.language.toLowerCase().startsWith("zh")) {
    return "zh-CN";
  }
  return "en";
}
