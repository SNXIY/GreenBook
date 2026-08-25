import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/context/AuthContext";
import AgentMarkdown from "@/components/content/AgentMarkdown";
import { commentService } from "@/services/commentService";
import type { CommentItem } from "@/types/comment";
import { AgentIcon, CheckIcon } from "@/components/icons/Icon";
import { agentService, waitForAgentRun } from "@/services/agentService";
import { waitForExecution } from "@/services/executionService";
import {
  approvalPresentation,
  projectExecutionActivity,
  projectRunActivity
} from "@/components/agent/userFacingResult";
import type { AgentRun } from "@/types/agent";
import type { Execution } from "@/types/execution";
import styles from "./CommentSection.module.css";
import { userFacingErrorMessage } from "@/services/userFacingError";
import { formatBusinessDateTime } from "@/utils/dateTime";

type Props = {
  postId: string;
  authorId?: string | number | null;
};

const formatTime = (value: string) => {
  return formatBusinessDateTime(value) ?? "";
};

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
  const [agentRun, setAgentRun] = useState<AgentRun | null>(null);
  const [agentExecution, setAgentExecution] = useState<Execution | null>(null);
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentExpanded, setAgentExpanded] = useState(true);
  const [agentTargetCommentId, setAgentTargetCommentId] = useState<string | null>(null);

  const accessToken = tokens?.accessToken;
  const canManageTop = !!user?.id && String(user.id) === String(authorId ?? "");
  const hotIds = new Set(hotItems.map(item => item.id));
  const normalItems = items.filter(item => !hotIds.has(item.id));
  const agentActivities = agentExecution
    ? projectExecutionActivity(agentExecution)
    : agentRun
      ? projectRunActivity(agentRun)
      : [];
  const approvalCopy = agentRun?.approval ? approvalPresentation(agentRun) : null;

  const load = useCallback(async (nextCursor?: string | null) => {
    setLoading(true);
    setError(null);
    try {
      const resp = await commentService.list(postId, undefined, nextCursor, 20, accessToken);
      setItems(prev => nextCursor ? [...prev, ...resp.items] : resp.items);
      setCursor(resp.nextCursor ?? null);
      setHasMore(resp.hasMore);
    } catch (err) {
      setError(userFacingErrorMessage(err, "评论暂时无法加载，请稍后重试。"));
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

  const finishAgentRun = async (result: AgentRun, targetCommentId: string | null) => {
    setAgentRun(result);
    if (result.status === "FAILED") {
      throw new Error("这次没有完成，请稍后重试。");
    }
    if (result.status === "WAITING_APPROVAL") {
      setAgentExpanded(true);
      return;
    }
    if (result.status === "CANCELLED") {
      setAgentRun(null);
      setAgentExpanded(false);
      setAgentTargetCommentId(null);
      return;
    }
    if (result.status !== "COMPLETED") return;

    await load(null);
    if (targetCommentId) {
      const response = await commentService.list(postId, targetCommentId, null, 20, accessToken);
      setReplies(previous => ({ ...previous, [targetCommentId]: response.items }));
      setExpandedReplies(previous => ({ ...previous, [targetCommentId]: true }));
    }
    setAgentRun(null);
    setAgentExpanded(false);
    setAgentTargetCommentId(null);
  };

  const askAgent = async (prompt: string, commentId: string) => {
    if (!accessToken || authLoading) return;
    setAgentBusy(true);
    setAgentRun(null);
    setAgentExecution(null);
    setAgentExpanded(true);
    setAgentTargetCommentId(commentId);
    setError(null);
    try {
      const conversations = await agentService.listConversations(accessToken, postId);
      const conversation = conversations[0] ?? await agentService.createConversation(accessToken, {
        context_post_id: postId,
        surface: "COMMENT"
      });
      const accepted = await agentService.send(
        accessToken,
        conversation.conversation_id,
        prompt,
        postId,
        commentId
      );
      if (accepted.execution_id) {
        const completed = await waitForExecution(
          accessToken,
          accepted.execution_id,
          setAgentExecution
        );
        if (completed.status === "FAILED") {
          throw new Error("这次没有完成，请稍后重试。");
        }
        if (["WAITING_APPROVAL", "WAITING_HUMAN", "PAUSED"].includes(completed.status)) {
          try {
            setAgentRun(await agentService.getRun(accessToken, accepted.run_id));
          } catch {
            // Keep the business activity card if the compatibility projection is unavailable.
          }
          return;
        }
        await load(null);
        setAgentExecution(null);
        setAgentExpanded(false);
        setAgentTargetCommentId(null);
        return;
      }
      const completed = await waitForAgentRun(accessToken, accepted.run_id, setAgentRun);
      await finishAgentRun(completed, commentId);
    } catch (err) {
      setError(userFacingErrorMessage(err, "回复暂时无法完成，请稍后重试。"));
    } finally {
      setAgentBusy(false);
    }
  };

  const decideAgentApproval = async (decision: "APPROVE" | "REJECT") => {
    if (!accessToken || !agentRun?.approval || agentBusy) return;
    const currentRun = agentRun;
    const approval = agentRun.approval;
    setAgentBusy(true);
    setError(null);
    try {
      const updated = await agentService.decideApproval(
        accessToken,
        currentRun.run_id,
        approval.approval_id,
        decision,
        approval.expected_run_version
      );
      setAgentRun(updated);
      if (decision === "REJECT") {
        await finishAgentRun(updated, agentTargetCommentId);
        return;
      }
      const completed = await waitForAgentRun(
        accessToken,
        currentRun.run_id,
        setAgentRun
      );
      await finishAgentRun(completed, agentTargetCommentId);
    } catch (err) {
      setError(userFacingErrorMessage(err, "确认操作暂时无法完成，请稍后重试。"));
    } finally {
      setAgentBusy(false);
    }
  };

  const submit = async () => {
    const text = content.trim();
    if (!text || !accessToken || authLoading) return;
    const mentionsAgent = /(?:^|\s)@(?:(?:知光|GreenBook)\s*)?Agent(?:\s|[，,：:]|$)/i.test(text);
    if (mentionsAgent && agentRun?.status === "WAITING_APPROVAL") {
      setError("请先确认或取消上一项发布任务，再交给Agent新的任务");
      setAgentExpanded(true);
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
      if (mentionsAgent) {
        void askAgent(text, created.id);
      }
    } catch (err) {
      setError(userFacingErrorMessage(err, "评论发布暂时无法完成，请稍后重试。"));
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
      setError(userFacingErrorMessage(err, "操作暂时无法完成，请稍后重试。"));
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
      setError(userFacingErrorMessage(err, "回复暂时无法加载，请稍后重试。"));
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
      setError(userFacingErrorMessage(err, "删除暂时无法完成，请稍后重试。"));
    }
  };

  const setTop = async (comment: CommentItem) => {
    if (!accessToken) return;
    try {
      await commentService.setTop(comment.id, !comment.top, accessToken);
      setItems(prev => prev.map(item => item.id === comment.id ? { ...item, top: !comment.top } : item));
      void loadHot();
    } catch (err) {
      setError(userFacingErrorMessage(err, "置顶暂时无法完成，请稍后重试。"));
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
            {comment.assistant ? <span className={styles.agentBadge}><AgentIcon width={12} height={12} aria-hidden="true" /> AI Agent</span> : null}
            {comment.top ? <span className={styles.badge}>置顶</span> : null}
            <span>{formatTime(comment.createTime)}</span>
          </div>
          {comment.assistant ? (
            <AgentMarkdown content={comment.content} />
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
          placeholder={accessToken ? "例如：@Agent 参照本帖创作一篇同主题帖子并发布…" : "登录后参与评论…"}
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
              disabled={authLoading || submitting || agentBusy || agentRun?.status === "WAITING_APPROVAL"}
              onClick={() => setContent(previous => previous.includes("@Agent") ? previous : `@Agent ${previous}`)}
            >
              <AgentIcon width={16} height={16} aria-hidden="true" />
              @Agent
            </button>
          ) : null}
          <button className={`${styles.button} ${styles.primary}`} type="button" disabled={!accessToken || authLoading || submitting || !content.trim()} onClick={submit}>
            {submitting ? "发布中…" : "发布评论"}
          </button>
        </div>
      </div>
      {agentBusy || agentRun || agentExecution ? (
        <aside className={styles.agentReply} aria-live="polite">
          <button
            className={styles.agentReplyHeader}
            type="button"
            aria-expanded={agentExpanded}
            onClick={() => setAgentExpanded(previous => !previous)}
          >
            <span><AgentIcon width={17} height={17} aria-hidden="true" /> GreenBook Agent</span>
            <small>
              {agentRun?.status === "WAITING_APPROVAL"
                ? "等待你的确认"
                : agentBusy
                  ? "正在阅读并处理"
                  : "查看处理进度"}
              <span aria-hidden="true">{agentExpanded ? "收起" : "展开"}</span>
            </small>
          </button>
          {agentExpanded ? (
            <div className={styles.agentReplyBody}>
              {agentActivities.length ? (
                <div className={styles.agentSteps}>
                  {agentActivities.map(item => (
                    <span key={item.id}>
                      {item.status === "complete" ? <CheckIcon width={13} height={13} aria-hidden="true" /> : <i aria-hidden="true" />}
                      {item.label}
                    </span>
                  ))}
                </div>
              ) : null}
              {agentBusy && !agentActivities.length ? <p className={styles.muted}>正在准备回复…</p> : null}
              {approvalCopy ? (
                <div className={styles.agentApproval}>
                  <div>
                    <strong>{approvalCopy.actionTitle}</strong>
                    <p>{approvalCopy.resourceTitle}。{approvalCopy.consequence}</p>
                  </div>
                  <div className={styles.approvalActions}>
                    <button type="button" disabled={agentBusy} onClick={() => void decideAgentApproval("REJECT")}>
                      取消任务
                    </button>
                    <button type="button" disabled={agentBusy} onClick={() => void decideAgentApproval("APPROVE")}>
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
