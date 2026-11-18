import { memo } from "react";
import katex from "katex";

export default memo(function KatexMarkup({ latex, display }: { latex: string; display: boolean }) {
  return <span className="math" dangerouslySetInnerHTML={{
    __html: katex.renderToString(latex, { throwOnError: false, displayMode: display }),
  }} />;
});
