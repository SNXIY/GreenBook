import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useAuth } from "@/context/AuthContext";
import { commentService } from "@/services/commentService";
import type { CommentItem } from "@/types/comment";
import { AssistantIcon, CheckIcon } from "@/components/icons/Icon";
import { assistantService, waitForAssistantRun } from "@/services/assistantService";
import type { AssistantRun } from "@/types/assistant";
import styles from "./CommentSection.module.css";

type Props = {
  postId: string;
  authorId?: string | number | null;
};

const formatTime = (value: string) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString("zh-CN", { hour12: false });
};

const AssistantMarkdown = ({ content }: { content: string }) => (
  <div className={styles.assistantMarkdown}>
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ node, ...props }) => (
          <a {...props} target="_blank" rel="noreferrer" />
        ),
        img: ({ src, alt }) => (
          <a href={src} target="_blank" rel="noreferrer">
            {alt?.trim() || "查看图片"}
          </a>
        )
      }}
    >
      {content}
    </ReactMarkdown>
  </div>
);

const CommentSection = ({ postId, authorId }: Props) => {
  const { tokens, user, isLoading: authLoading } = useAuth();
  const [content, setContent] = useState("");
  const [items, setItems] = useState<CommentItem[]>([]);
  const [hotItems, setHotItems] = useState<CommentItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [replyTo, setReplyTo] = useState<CommentItem | null>(null);
  const [replies, setReplies] = useState<Record<string, CommentItem[]>>({});
  const [expandedReplies, setExpandedReplies] = useState<Record<string, boolean>>({});
  const [loadingReplies, setLoadingReplies] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [assistantRun, setAssistantRun] = useState<AssistantRun | null>(null);
  const [assistantBusy, setAssistantBusy] = useState(false);
  const [assistantExpanded, setAssistantExpanded] = useState(true);
  const [assistantTargetCommentId, setAssistantTargetCommentId] = useState<string | null>(null);

  const accessToken = tokens?.accessToken;
  const canManageTop = !!user?.id && String(user.id) === String(authorId ?? "");
  const hotIds = new Set(hotItems.map(item => item.id));
  const normalItems = items.filter(item => !hotIds.has(item.id));

  const load = useCallback(async (nextCursor?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await commentService.list(postId, undefined, nextCursor, 20, accessToken);
      setItems(prev => nextCursor ? [...prev, ...resp.items] : resp.items);
      setCursor(resp.nextCursor ?? null);
      setHasMore(resp.hasMore);
    } catch (err) {
      setError(err instanceof Error ? err.message : "评论加载失败");
    } finally {
      setLoading(false);
    }
  }, [accessToken, postId]);

  const loadHot = useCallback(async () => {
    try {
      const resp = await commentService.hot(postId, 5, accessToken);
      setHotItems(resp);
    } catch {
      setHotItems([]);
    }
  }, [accessToken, postId]);

  useEffect(() => {
    void load(null);
    void loadHot();
  }, [load, loadHot]);

  const finishAssistantRun = async (result: AssistantRun, targetCommentId: string | null) => {
    setAssistantRun(result);
    if (result.status === "FAILED") {
      throw new Error(result.error || "助手任务执行失败");
    }
    if (result.status === "WAITING_APPROVAL") {
      setAssistantExpanded(true);
      return;
    }
    if (result.status === "CANCELLED") {
      setAssistantRun(null);
      setAssistantExpanded(false);
      setAssistantTargetCommentId(null);
      return;
    }
    if (result.status !== "COMPLETED") return;

    await load(null);
    if (targetCommentId) {
      const response = await commentService.list(postId, targetCommentId, null, 20, accessToken);
      setReplies(previous => ({ ...previous, [targetCommentId]: response.items }));
      setExpandedReplies(previous => ({ ...previous, [targetCommentId]: true }));
    }
    setAssistantRun(null);
    setAssistantExpanded(false);
    setAssistantTargetCommentId(null);
  };

  const askAssistant = async (prompt: string, commentId: string) => {
    if (!accessToken || authLoading) return;
    setAssistantBusy(true);
    setAssistantRun(null);
    setAssistantExpanded(true);
    setAssistantTargetCommentId(commentId);
    setError(null);
    try {
      const conversations = await assistantService.listConversations(accessToken, postId);
      const conversation = conversations[0] ?? await assistantService.createConversation(accessToken, {
        context_post_id: postId,
        surface: "COMMENT"
      });
      const accepted = await assistantService.send(
        accessToken,
        conversation.conversation_id,
        prompt,
        postId,
        commentId
      );
      const completed = await waitForAssistantRun(accessToken, accepted.run_id, setAssistantRun);
      await finishAssistantRun(completed, commentId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "助手回复失败");
    } finally {
      setAssistantBusy(false);
    }
  };

  const decideAssistantApproval = async (decision: "APPROVE" | "REJECT") => {
    if (!accessToken || !assistantRun?.approval || assistantBusy) return;
    const currentRun = assistantRun;
    const approval = assistantRun.approval;
    setAssistantBusy(true);
    setError(null);
    try {
      const updated = await assistantService.decideApproval(
        accessToken,
        currentRun.run_id,
        approval.approval_id,
        decision,
        approval.expected_run_version
      );
      setAssistantRun(updated);
      if (decision === "REJECT") {
        await finishAssistantRun(updated, assistantTargetCommentId);
        return;
      }
      const completed = await waitForAssistantRun(
        accessToken,
        currentRun.run_id,
        setAssistantRun
      );
      await finishAssistantRun(completed, assistantTargetCommentId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "发布确认失败");
    } finally {
      setAssistantBusy(false);
    }
  };

  const submit = async () => {
    const text = content.trim();
    if (!text || !accessToken || authLoading) return;
    const mentionsAssistant = /(?:^|\s)@(?:(?:知光|GREEN-BOOK)\s*)?助手(?:\s|[，,：:]|$)/i.test(text);
    if (mentionsAssistant && assistantRun?.status === "WAITING_APPROVAL") {
      setError("请先确认或取消上一项发布任务，再交给助手新的任务");
      setAssistantExpanded(true);
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const created = await commentService.create({
        postId,
        parentId: replyTo?.id,
        content: text
      }, accessToken);
      if (replyTo) {
        setReplies(prev => ({
          ...prev,
          [replyTo.id]: [...(prev[replyTo.id] ?? []), created]
        }));
        setExpandedReplies(previous => ({ ...previous, [replyTo.id]: true }));
        setItems(prev => prev.map(item => item.id === replyTo.id ? { ...item, replyCount: item.replyCount + 1 } : item));
      } else {
        setItems(prev => [created, ...prev]);
      }
      setContent("");
      setReplyTo(null);
      void loadHot();
      if (mentionsAssistant) {
        void askAssistant(text, created.id);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "评论发布失败");
    } finally {
      setSubmitting(false);
    }
  };

  const toggleLike = async (comment: CommentItem) => {
    if (!accessToken) return;
    const liked = comment.liked;
    const apply = (target: CommentItem) => target.id === comment.id
      ? { ...target, liked: !liked, likeCount: Math.max(0, target.likeCount + (liked ? -1 : 1)) }
      : target;
    setItems(prev => prev.map(apply));
    setHotItems(prev => prev.map(apply));
    setReplies(prev => Object.fromEntries(Object.entries(prev).map(([key, list]) => [key, list.map(apply)])));
    try {
      liked ? await commentService.unlike(comment.id, accessToken) : await commentService.like(comment.id, accessToken);
      void loadHot();
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    }
  };

  const toggleReplies = async (commentId: string) => {
    if (expandedReplies[commentId]) {
      setExpandedReplies(previous => ({ ...previous, [commentId]: false }));
      return;
    }
    setExpandedReplies(previous => ({ ...previous, [commentId]: true }));
    if (Object.prototype.hasOwnProperty.call(replies, commentId)) return;
    setLoadingReplies(previous => ({ ...previous, [commentId]: true }));
    try {
      const resp = await commentService.list(postId, commentId, null, 20, accessToken);
      setReplies(prev => ({ ...prev, [commentId]: resp.items }));
    } catch (err) {
      setExpandedReplies(previous => ({ ...previous, [commentId]: false }));
      setError(err instanceof Error ? err.message : "回复加载失败");
    } finally {
      setLoadingReplies(previous => ({ ...previous, [commentId]: false }));
    }
  };

  const remove = async (comment: CommentItem) => {
    if (!accessToken) return;
    try {
      await commentService.remove(comment.id, accessToken);
      setItems(prev => prev.filter(item => item.id !== comment.id));
      setHotItems(prev => prev.filter(item => item.id !== comment.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败");
    }
  };

  const setTop = async (comment: CommentItem) => {
    if (!accessToken) return;
    try {
      await commentService.setTop(comment.id, !comment.top, accessToken);
      setItems(prev => prev.map(item => item.id === comment.id ? { ...item, top: !comment.top } : item));
      void loadHot();
    } catch (err) {
      setError(err instanceof Error ? err.message : "置顶失败");
    }
  };

  const renderComment = (comment: CommentItem, nested = false) => {
    const canDelete = !!user?.id && String(user.id) === comment.userId;
    const childReplies = replies[comment.id] ?? [];
    const repliesExpanded = !!expandedReplies[comment.id];
    const repliesLoading = !!loadingReplies[comment.id];
    const hasReplies = comment.replyCount > 0 || childReplies.length > 0;
    const visibleReplyCount = Math.max(comment.replyCount, childReplies.length);
    return (
      <div key={comment.id} className={styles.comment}>
        {comment.authorAvatar ? <img className={styles.avatar} src={comment.authorAvatar} alt="" width={36} height={36} loading="lazy" /> : <div className={styles.avatar} />}
        <div className={styles.body}>
          <div className={styles.meta}>
            <span className={styles.author}>{comment.authorNickname}</span>
            {comment.assistant ? <span className={styles.assistantBadge}><AssistantIcon width={12} height={12} aria-hidden="true" /> AI 助手</span> : null}
            {comment.top ? <span className={styles.badge}>置顶</span> : null}
            <span>{formatTime(comment.createTime)}</span>
          </div>
          {comment.assistant ? (
            <AssistantMarkdown content={comment.content} />
          ) : (
            <div className={styles.content}>{comment.content}</div>
          )}
          <div className={styles.tools}>
            <button className={`${styles.tool} ${comment.liked ? styles.toolActive : ""}`} type="button" onClick={() => toggleLike(comment)}>
              {comment.liked ? "已赞" : "点赞"} {comment.likeCount}
            </button>
            {!comment.assistant ? <button className={styles.tool} type="button" onClick={() => setReplyTo(comment)}>回复</button> : null}
            {hasReplies ? (
              <button
                className={styles.tool}
                type="button"
                aria-expanded={repliesExpanded}
                aria-controls={`comment-replies-${comment.id}`}
                disabled={repliesLoading}
                onClick={() => void toggleReplies(comment.id)}
              >
                {repliesLoading
                  ? "加载回复…"
                  : repliesExpanded
                    ? "收起回复"
                    : `查看回复 ${visibleReplyCount}`}
              </button>
            ) : null}
            {canDelete ? <button className={styles.tool} type="button" onClick={() => remove(comment)}>删除</button> : null}
            {!nested && canManageTop ? <button className={styles.tool} type="button" onClick={() => setTop(comment)}>{comment.top ? "取消置顶" : "置顶"}</button> : null}
          </div>
          {repliesExpanded && childReplies.length ? (
            <div className={styles.replies} id={`comment-replies-${comment.id}`}>
              {childReplies.map(reply => renderComment(reply, true))}
            </div>
          ) : null}
        </div>
      </div>
    );
  };

  return (
    <section className={styles.section}>
      <h2>评论</h2>
      {hotItems.length ? (
        <div className={styles.list}>
          <div className={styles.muted}>热评</div>
          {hotItems.map(item => renderComment(item))}
        </div>
      ) : null}
      <div className={styles.composer}>
        {replyTo ? <div className={styles.muted}>回复 {replyTo.authorNickname} <button className={styles.tool} type="button" onClick={() => setReplyTo(null)}>取消</button></div> : null}
        <textarea
          className={styles.textarea}
          placeholder={accessToken ? "例如：@助手 参照本帖创作一篇同主题帖子并发布…" : "登录后参与评论…"}
          name="comment-content"
          aria-label="评论内容"
          autoComplete="off"
          value={content}
          disabled={!accessToken || authLoading || submitting}
          onChange={e => setContent(e.target.value)}
        />
        <div className={styles.actions}>
          {accessToken ? (
            <button
              className={styles.mentionButton}
              type="button"
              disabled={authLoading || submitting || assistantBusy || assistantRun?.status === "WAITING_APPROVAL"}
              onClick={() => setContent(previous => previous.includes("@助手") ? previous : `@助手 ${previous}`)}
            >
              <AssistantIcon width={16} height={16} aria-hidden="true" />
              @助手
            </button>
          ) : null}
          <button className={`${styles.button} ${styles.primary}`} type="button" disabled={!accessToken || authLoading || submitting || !content.trim()} onClick={submit}>
            {submitting ? "发布中…" : "发布评论"}
          </button>
        </div>
      </div>
      {assistantBusy || assistantRun ? (
        <aside className={styles.assistantReply} aria-live="polite">
          <button
            className={styles.assistantReplyHeader}
            type="button"
            aria-expanded={assistantExpanded}
            onClick={() => setAssistantExpanded(previous => !previous)}
          >
            <span><AssistantIcon width={17} height={17} aria-hidden="true" /> GREEN-BOOK 助手</span>
            <small>
              {assistantRun?.status === "WAITING_APPROVAL"
                ? "等待你的确认"
                : assistantBusy
                  ? "正在阅读并处理"
                  : "查看执行进度"}
              <span aria-hidden="true">{assistantExpanded ? "收起" : "展开"}</span>
            </small>
          </button>
          {assistantExpanded ? (
            <div className={styles.assistantReplyBody}>
              {assistantRun?.steps.length ? (
                <div className={styles.assistantSteps}>
                  {assistantRun.steps.map(step => (
                    <span key={step.step_id}>
                      {step.status === "COMPLETED" ? <CheckIcon width={13} height={13} aria-hidden="true" /> : <i aria-hidden="true" />}
                      {step.label}
                    </span>
                  ))}
                </div>
              ) : null}
              {assistantBusy && !assistantRun?.steps.length ? <p className={styles.muted}>正在建立任务…</p> : null}
              {assistantRun?.approval ? (
                <div className={styles.assistantApproval}>
                  <div>
                    <strong>公开发布前需要你确认</strong>
                    <p>{assistantRun.approval.description}</p>
                  </div>
                  <div className={styles.approvalActions}>
                    <button type="button" disabled={assistantBusy} onClick={() => void decideAssistantApproval("REJECT")}>
                      取消任务
                    </button>
                    <button type="button" disabled={assistantBusy} onClick={() => void decideAssistantApproval("APPROVE")}>
                      确认发布
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          ) : null}
        </aside>
      ) : null}
      {error ? <div className={styles.error} role="alert">{error}</div> : null}
      <div className={styles.list}>
        {hotItems.length ? <div className={styles.muted}>全部评论</div> : null}
        {normalItems.map(item => renderComment(item))}
        {loading ? <div className={styles.muted}>加载中…</div> : null}
        {!loading && !items.length && !hotItems.length ? <div className={styles.muted}>暂无评论</div> : null}
        {hasMore ? <button className={styles.button} type="button" onClick={() => load(cursor)}>加载更多</button> : null}
      </div>
    </section>
  );
};

export default CommentSection;
