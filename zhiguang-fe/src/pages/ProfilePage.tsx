import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import MainHeader from "@/components/layout/MainHeader";
import SectionHeader from "@/components/common/SectionHeader";
import AuthStatus from "@/features/auth/AuthStatus";
import { useAuth } from "@/context/AuthContext";
import styles from "./ProfilePage.module.css";
import feedStyles from "./HomePage.module.css";
import CourseCard from "@/components/cards/CourseCard";
import LikeFavBar from "@/components/common/LikeFavBar";
import { knowpostService } from "@/services/knowpostService";
import RelationCounters from "@/components/common/RelationCounters";
import type { AgentDraft, ScheduledPublication } from "@/types/knowpost";
import { userFacingErrorMessage } from "@/services/userFacingError";
import { formatBusinessDateTime } from "@/utils/dateTime";

const scheduleForDraft = (
  schedules: ScheduledPublication[],
  draftId: string
): ScheduledPublication | undefined => schedules
  .filter(schedule => schedule.draftId === draftId)
  .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))[0];

const contentStatus = (schedule?: ScheduledPublication) => {
  const state = schedule?.status?.toUpperCase();
  if (state === "SCHEDULED" || state === "PENDING") {
    const runAt = formatBusinessDateTime(schedule?.runAt, schedule?.timezone);
    return { label: "已定时", detail: runAt ? `发布于 ${runAt}` : "" };
  }
  if (state === "PROCESSING" || state === "RUNNING" || state === "RECONCILING") {
    return { label: state === "RECONCILING" ? "正在确认结果…" : "处理中…", detail: "" };
  }
  if (state === "FAILED") {
    return { label: "发布未完成", detail: "请稍后重试。" };
  }
  if (state === "PUBLISHED") {
    return { label: "已发布", detail: "" };
  }
  return { label: "草稿", detail: "" };
};

const ProfilePage = () => {
  const { user, tokens } = useAuth();
  const displayName = user?.nickname ?? user?.phone ?? user?.email ?? "GreenBook 用户";
  const avatarInitial = displayName.trim().charAt(0) || "知";

  // 领域标签展示：仅解析 tagJson
  const tags = useMemo(() => {
    if (user && typeof user.tagJson === "string") {
      try {
        const parsed = JSON.parse(user.tagJson);
        return Array.isArray(parsed)
          ? parsed.filter((t) => typeof t === "string")
          : [];
      } catch {
        return [];
      }
    }
    return [];
  }, [user]);


  // 我的知文列表数据
  const [items, setItems] = useState<Array<{
    id: string;
    title: string;
    description: string;
    coverImage?: string;
    tags: string[];
    tagJson?: string;
    authorAvatar?: string;
    authorAvator?: string;
    authorNickname: string;
    likeCount?: number;
    favoriteCount?: number;
    liked?: boolean;
    faved?: boolean;
    isTop?: boolean;
  }>>([]);
  const [drafts, setDrafts] = useState<AgentDraft[]>([]);
  const [schedules, setSchedules] = useState<ScheduledPublication[]>([]);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // 抽取成单一函数，供首次加载与编辑动作后复用
  const reloadMine = useCallback(async (silent = false) => {
    if (!tokens?.accessToken) return;
    if (!silent) setLoading(true);
    if (!silent) setError(null);
    try {
      const [resp, draftResponse, scheduleResponse] = await Promise.all([
        knowpostService.mine(1, 20, tokens.accessToken),
        knowpostService.myDrafts(tokens.accessToken),
        knowpostService.mySchedules(tokens.accessToken)
      ]);
      setItems(resp.items ?? []);
      setDrafts(draftResponse ?? []);
      setSchedules(scheduleResponse ?? []);
      setHasMore(!!resp.hasMore);
      setPage(resp.page ?? 1);
    } catch (err) {
      setError(userFacingErrorMessage(err, "内容暂时无法加载，请稍后重试。"));
    } finally {
      if (!silent) setLoading(false);
    }
  }, [tokens?.accessToken]);

  useEffect(() => {
    void reloadMine();
  }, [reloadMine]);

  const hasPendingBusinessWork = schedules.some(schedule =>
    ["SCHEDULED", "PENDING", "PROCESSING", "RUNNING", "RECONCILING"].includes(
      schedule.status.toUpperCase()
    )
  );

  useEffect(() => {
    if (!tokens?.accessToken || !hasPendingBusinessWork) return;
    const refresh = () => {
      if (document.visibilityState === "visible") void reloadMine(true);
    };
    const timer = window.setInterval(refresh, 10_000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, [hasPendingBusinessWork, reloadMine, tokens?.accessToken]);


  return (
    <AppLayout
      header={
        <MainHeader
          headline="我的内容"
          subtitle="草稿、定时发布和已发布内容都以服务端真实状态为准"
          rightSlot={<AuthStatus />}
        />
      }
    >
      <>
        <SectionHeader
          title="个人信息"
          subtitle="让同学们更快认识你"
          actions={<Link to="/profile/edit" className="ghost-button">编辑资料</Link>}
        />
        <div className={styles.profileGrid}>
          <div className={styles.avatarBox}>
            {user?.avatar ? (
              <img src={user.avatar} alt="avatar" className={styles.avatarImg} />
            ) : (
              <span>{avatarInitial}</span>
            )}
          </div>
          <div className={styles.infoBox}>
            <div className={styles.nickname}>{displayName}</div>
            <div className={styles.tags}>
              {tags.length > 0 ? (
                tags.map(tag => <span key={tag}>{tag}</span>)
              ) : (
                <span>未设置</span>
              )}
            </div>
          </div>
        </div>
        <div className={styles.bioBlock}>{user?.bio ?? "暂无简介"}</div>

        {/* 关系计数展示 */}
        {user?.id ? (
          <div style={{ marginTop: 8 }}>
            <RelationCounters userId={user.id} />
          </div>
        ) : null}

        <section id="my-content">
        <SectionHeader title="我的内容" subtitle="草稿、定时发布和已发布内容以实际业务状态为准" />
        {error ? <div style={{ color: "var(--color-danger)" }} role="alert">{error}</div> : null}
        {!user ? (
          <div style={{ color: "var(--color-text-muted)", padding: 12 }}>请登录后查看你的知文</div>
        ) : (
          <>
          {drafts.length ? (
            <ul className={styles.contentList}>
              {drafts.map(draft => {
                const status = contentStatus(scheduleForDraft(schedules, draft.draftId));
                return (
                  <li key={draft.draftId} className={styles.contentItem}>
                    <div className={styles.contentMeta}>
                      <strong className={styles.contentTitle}>{draft.title || "未命名草稿"}</strong>
                      {draft.summary ? <span>{draft.summary}</span> : null}
                      <span className={styles.contentStats}>{status.label}{status.detail ? ` · ${status.detail}` : ""}</span>
                    </div>
                    <Link className={styles.smallButton} to={`/create/manual?draftId=${encodeURIComponent(draft.draftId)}`}>编辑</Link>
                  </li>
                );
              })}
            </ul>
          ) : null}
          <div className={feedStyles.masonry}>
            {items.map(item => (
              <div key={item.id} className={feedStyles.masonryItem}>
                <span className={styles.publishedStatus}>已发布</span>
                <CourseCard
                  id={item.id}
                  title={item.title}
                  summary={item.description ?? ""}
                  tags={item.tags ?? []}
                  isTop={item.isTop}
                  authorTags={(() => {
                    try {
                      return item.tagJson ? (JSON.parse(item.tagJson) as unknown[]).filter((t) => typeof t === "string") as string[] : [];
                    } catch {
                      return [];
                    }
                  })()}
                  teacher={{ name: item.authorNickname, avatarUrl: item.authorAvatar ?? item.authorAvator }}
                  coverImage={item.coverImage}
                  to={`/post/${item.id}`}
                  editable
                  onChanged={(action) => {
                    if (action === "delete") {
                      setItems(prev => prev.filter(x => x.id !== item.id));
                    } else {
                      void reloadMine();
                    }
                  }}
              footerExtra={<LikeFavBar entityId={item.id} compact initialCounts={{ like: item.likeCount ?? 0, fav: item.favoriteCount ?? 0 }} initialState={{ liked: item.liked, faved: item.faved }} />}
            />
              </div>
            ))}
            {loading ? <div className={feedStyles.masonryItem}><div>加载中…</div></div> : null}
            {!loading && items.length === 0 && drafts.length === 0 ? (
              <div className={feedStyles.masonryItem}><div>暂无内容</div></div>
            ) : null}
          </div>
          </>
        )}
        </section>
      </>
    </AppLayout>
  );
};

export default ProfilePage;
