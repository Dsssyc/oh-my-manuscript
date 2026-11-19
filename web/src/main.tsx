import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "katex/dist/katex.min.css";
import "./workbench.css";

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("ERR_DOM_ROOT_MISSING: #root element not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
