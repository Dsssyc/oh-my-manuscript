import { ApiError } from "./api";
import type { Translate } from "./i18n";

export function ErrorNotice({ error, t, dismiss }: { error: unknown; t: Translate; dismiss: () => void }) {
  if (error == null) return null;
  return <div className="notice error-notice" role="alert">
    <span>
      {error instanceof ApiError && <code>{error.code} </code>}
      {error instanceof Error && error.message ? error.message : t("error.request")}
    </span>
    <button type="button" className="quiet icon-button" aria-label={t("error.close")} onClick={dismiss}>×</button>
  </div>;
}
