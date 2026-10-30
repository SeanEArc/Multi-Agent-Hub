import { memo } from "react";
import Markdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

// GitHub-flavored markdown (tables, task lists, strikethrough) and highlight.js colors for
// fenced code blocks that name their language. Colors are in styles.css (.hljs-*).
const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];

/**
 * Markdown from an agent, a brief or a summary. Memoized on its text, so a long transcript
 * doesn't parse and highlight every message again each time an event arrives.
 */
export const MarkdownText = memo(function MarkdownText({ children }: { children: string }) {
  return (
    <Markdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins}>
      {children}
    </Markdown>
  );
});
