import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import styles from "./AssistantMarkdown.module.css";

type Props = {
  content: string;
};

const AssistantMarkdown = ({ content }: Props) => (
  <div className={styles.markdown}>
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      skipHtml
      components={{
        a: ({ node, ...props }) => (
          <a {...props} target="_blank" rel="noreferrer" />
        ),
        img: ({ src, alt }) => (
          src ? (
            <a href={src} target="_blank" rel="noreferrer">
              {alt?.trim() || "查看图片"}
            </a>
          ) : (
            <span>{alt?.trim() || "图片"}</span>
          )
        )
      }}
    >
      {content}
    </ReactMarkdown>
  </div>
);

export default AssistantMarkdown;
