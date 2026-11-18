import { useCallback, useEffect, useRef, useState } from "react";
import { api, subscribe, type WorkspaceEvent, type WorkspaceSummary } from "./api";
import { detectBrowserLocale, translate } from "./i18n";
import { ErrorNotice } from "./ErrorNotice";
import { Workbench } from "./Workbench";

function workspaceFromHash() {
  const match = /^#workspaces\/([^/]+)$/.exec(window.location.hash);
  if (!match) return undefined;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return undefined;
  }
}

export function App() {
  const [locale, setLocale] = useState(detectBrowserLocale);
  const [hashId, setHashId] = useState(workspaceFromHash);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>();
  const [error, setError] = useState<unknown>();
  const [listVersion, setListVersion] = useState(0);
  const listener = useRef<(event: WorkspaceEvent) => void>(null);
  const t = translate(locale);
  const selected = hashId ?? workspaces?.[0]?.workspace_id;
  const summary = workspaces?.find((workspace) => workspace.workspace_id === selected);

  useEffect(() => {
    const onHash = () => setHashId(workspaceFromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => subscribe((event) => {
    if (event.type === "workspaces_changed" || event.type === "reconnected") setListVersion((value) => value + 1);
    listener.current?.(event);
  }), []);

  useEffect(() => {
    const controller = new AbortController();
    setError(undefined);
    void api.workspaces(controller.signal).then(({ workspaces: list }) => {
      if (!controller.signal.aborted) setWorkspaces(list);
    }).catch((reason: unknown) => {
      if (!controller.signal.aborted) setError(reason);
    });
    return () => controller.abort();
  }, [listVersion]);

  useEffect(() => { document.documentElement.lang = locale; }, [locale]);

  const listen = useCallback((callback: (event: WorkspaceEvent) => void) => {
    listener.current = callback;
    return () => { if (listener.current === callback) listener.current = null; };
  }, []);

  function toggleLocale() {
    const next = locale === "en" ? "zh-CN" : "en";
    const url = new URL(window.location.href);
    url.searchParams.set("lang", next);
    window.history.replaceState(null, "", url);
    setLocale(next);
  }

  return <div className="app">
    <header className="masthead">
      <span className="wordmark">{t("app.title")}</span>
      <select className="workspace-select" aria-label={t("workspace.label")} value={selected ?? ""}
        disabled={!selected} onChange={(event) => {
          window.location.hash = `workspaces/${encodeURIComponent(event.target.value)}`;
          setHashId(event.target.value);
        }}>
        {!selected && <option value="">{t("workspace.label")}</option>}
        {selected && !summary &&
          <option value={selected}>{selected}</option>}
        {workspaces?.map((workspace) => (
          <option key={workspace.workspace_id} value={workspace.workspace_id}>{workspace.title || workspace.path}</option>
        ))}
      </select>
      <button type="button" className="quiet language" aria-label={t("app.language")} onClick={toggleLocale}>
        {t("app.lang_toggle")}
      </button>
    </header>
    <ErrorNotice error={error} t={t} dismiss={() => setError(undefined)} />
    {selected ? <Workbench key={selected} workspaceId={selected} summaryUnsynced={summary?.has_unsynced_changes}
      t={t} listen={listen} /> : (
      <main className="empty">
        <p>{t(error == null && !workspaces ? "workspace.loading" : "empty.workspace")}</p>
        {error != null && <button type="button" onClick={() => setListVersion((value) => value + 1)}>{t("workspace.retry")}</button>}
      </main>
    )}
  </div>;
}
