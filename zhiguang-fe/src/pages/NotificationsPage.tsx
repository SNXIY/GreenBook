import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import MainHeader from "@/components/layout/MainHeader";
import AuthStatus from "@/features/auth/AuthStatus";
import { useAuth } from "@/context/AuthContext";
import { notificationService } from "@/services/notificationService";
import { emitNotificationUnreadChanged } from "@/services/notificationEvents";
import type { NotificationItem } from "@/types/notification";
import styles from "./NotificationsPage.module.css";

const typeLabel: Record<string, string> = {
  LIKE: "点赞",
  FAVORITE: "收藏",
  COMMENT: "评论",
  REPLY: "回复",
  FOLLOW: "关注"
};

const NotificationsPage = () => {
  const { tokens } = useAuth();
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unreadCount, setUnreadCount] = useState(0);

  const load = useCallback(async (nextCursor?: string | null) => {
    if (!tokens?.accessToken) return;
    setLoading(true);
    setError(null);
    try {
      const resp = await notificationService.list(nextCursor, 20);
      setItems(prev => nextCursor ? [...prev, ...(resp.items ?? [])] : resp.items ?? []);
      setCursor(resp.nextCursor ?? null);
      setHasMore(!!resp.hasMore);
    } catch (err) {
      setError(err instanceof Error ? err.message : "通知加载失败");
    } finally {
      setLoading(false);
    }
  }, [tokens?.accessToken]);

  const refreshUnread = useCallback(async () => {
    if (!tokens?.accessToken) return;
    const resp = await notificationService.unreadCount();
    setUnreadCount(resp.unreadCount ?? 0);
  }, [tokens?.accessToken]);

  useEffect(() => {
    load(null);
    refreshUnread();
  }, [load, refreshUnread]);

  const markAllRead = async () => {
    await notificationService.markAllRead();
    setItems(prev => prev.map(item => ({ ...item, read: true })));
    setUnreadCount(0);
    emitNotificationUnreadChanged(0);
  };

  const markOneRead = async (id: string) => {
    const wasUnread = items.some(item => item.id === id && !item.read);
    await notificationService.markRead([id]);
    setItems(prev => prev.map(item => item.id === id ? { ...item, read: true } : item));
    setUnreadCount(prev => {
      const next = wasUnread ? Math.max(0, prev - 1) : prev;
      emitNotificationUnreadChanged(next);
      return next;
    });
  };

  const targetLink = (item: NotificationItem) => {
    if (item.type === "FOLLOW" && item.actorId) return `/profile?userId=${item.actorId}`;
    if (item.aggregateType === "post" && item.aggregateId) return `/post/${item.aggregateId}`;
    if (item.targetType === "post") return `/post/${item.targetId}`;
    return "/";
  };

  return (
    <AppLayout
      header={
        <MainHeader
          headline="消息通知"
          subtitle={unreadCount > 0 ? `${unreadCount} 条未读消息` : "暂无未读消息"}
          rightSlot={<AuthStatus />}
        />
      }
    >
      <section className={styles.toolbar}>
        <button type="button" onClick={markAllRead} disabled={unreadCount === 0}>
          全部已读
        </button>
      </section>

      {error ? <div className={styles.error}>{error}</div> : null}

      <div className={styles.list}>
        {items.map(item => (
          <article key={item.id} className={`${styles.item} ${item.read ? "" : styles.unread}`}>
            <div className={styles.avatar}>
              {item.actorAvatar ? <img src={item.actorAvatar} alt="" /> : <span>{(item.actorName ?? "知").slice(0, 1)}</span>}
            </div>
            <div className={styles.body}>
              <div className={styles.meta}>
                <span className={styles.type}>{typeLabel[item.type] ?? item.type}</span>
                <span>{new Date(item.createTime).toLocaleString()}</span>
              </div>
              <Link to={targetLink(item)} className={styles.title} onClick={() => !item.read && markOneRead(item.id)}>
                {item.actorName ? `${item.actorName} · ` : ""}
                {item.actorCount > 1 ? `${item.actorCount} 人` : item.title}
              </Link>
              {item.content ? <p>{item.content}</p> : null}
            </div>
            {!item.read ? <span className={styles.dot} /> : null}
          </article>
        ))}

        {!loading && items.length === 0 ? <div className={styles.empty}>暂无通知</div> : null}
      </div>

      {hasMore ? (
        <button type="button" className={styles.more} onClick={() => load(cursor)} disabled={loading}>
          {loading ? "加载中…" : "加载更多"}
        </button>
      ) : null}
    </AppLayout>
  );
};

export default NotificationsPage;
