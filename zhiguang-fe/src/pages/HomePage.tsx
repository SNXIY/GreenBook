import { useCallback, useEffect, useState } from "react";
import AppLayout from "@/components/layout/AppLayout";
import MainHeader from "@/components/layout/MainHeader";
import CourseCard from "@/components/cards/CourseCard";
import LikeFavBar from "@/components/common/LikeFavBar";
import { knowpostService } from "@/services/knowpostService";
import AuthStatus from "@/features/auth/AuthStatus";
import AssistantPanel from "@/components/assistant/AssistantPanel";
import { AssistantIcon } from "@/components/icons/Icon";
import { useAuth } from "@/context/AuthContext";
import type { FeedItem } from "@/types/knowpost";
import styles from "./HomePage.module.css";

type FeedMode = "recommend" | "following";

const HomePage = () => {
  const { tokens } = useAuth();
  const [mode, setMode] = useState<FeedMode>("recommend");
  const [items, setItems] = useState<FeedItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const [hasMore, setHasMore] = useState(true);
  const [assistantOpen, setAssistantOpen] = useState(false);
  const size = 20;

  const fetchPage = useCallback(async (currentPage: number) => {
    if (!hasMore || loading) return;
    if (mode === "following" && !tokens?.accessToken) {
      setError("请先登录后查看关注 Feed");
      setHasMore(false);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const resp = mode === "following"
        ? await knowpostService.followingFeed(currentPage, size, tokens?.accessToken ?? "")
        : await knowpostService.recommendFeed(currentPage, size);

      setItems(prev => currentPage === 1 ? resp.items ?? [] : [...prev, ...(resp.items ?? [])]);
      setHasMore(!!resp.hasMore);
      setPage(currentPage + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [hasMore, loading, mode, tokens?.accessToken]);

  const switchMode = (nextMode: string) => {
    setMode(nextMode as FeedMode);
    setItems([]);
    setPage(1);
    setHasMore(true);
    setError(null);
  };

  useEffect(() => {
    void fetchPage(1);
  }, [mode]);

  useEffect(() => {
    const handleScroll = () => {
      const h = document.documentElement;
      const bottom = h.scrollTop + h.clientHeight + 200 >= h.scrollHeight;
      if (bottom) {
        void fetchPage(page);
      }
    };

    window.addEventListener("scroll", handleScroll);
    return () => window.removeEventListener("scroll", handleScroll);
  }, [fetchPage, page]);

  return (
    <>
      <AppLayout
      header={
        <MainHeader
          headline="让经验被看见，让知识继续生长"
          subtitle="来自真实创作者的观察、方法与长期实践。"
          tabs={[
            { id: "recommend", label: "推荐", active: mode === "recommend", onSelect: switchMode },
            { id: "following", label: "关注", active: mode === "following", onSelect: switchMode }
          ]}
          rightSlot={<AuthStatus />}
        />
      }
    >
      {error ? (
        <div className={styles.error} role="alert">
          <span>{error}</span>
          <button type="button" onClick={() => void fetchPage(page === 1 ? 1 : page)}>
            重新加载
          </button>
        </div>
      ) : null}

      <div className={styles.masonry}>
        {items.map(item => (
          <div key={item.id} className={styles.masonryItem}>
            <CourseCard
              id={item.id}
              title={item.title}
              summary={item.description ?? ""}
              tags={item.tags ?? []}
              authorTags={(() => {
                try {
                  return item.tagJson ? JSON.parse(item.tagJson) : [];
                } catch {
                  return [];
                }
              })()}
              teacher={{ name: item.authorNickname, avatarUrl: item.authorAvatar ?? item.authorAvator }}
              coverImage={item.coverImage}
              to={`/post/${item.id}`}
              footerExtra={
                <LikeFavBar
                  entityId={item.id}
                  compact
                  initialCounts={{ like: item.likeCount ?? 0, fav: item.favoriteCount ?? 0 }}
                  initialState={{ liked: item.liked, faved: item.faved }}
                />
              }
            />
          </div>
        ))}

        {loading
          ? Array.from({ length: items.length ? 1 : 4 }).map((_, index) => (
              <div className={styles.masonryItem} key={`skeleton-${index}`}>
                <div className={styles.skeleton} aria-label="正在加载内容">
                  <span />
                  <span />
                  <span />
                  <span />
                </div>
              </div>
            ))
          : null}

        {!loading && !error && items.length === 0 ? (
          <div className={styles.empty}>
            <strong>这里还没有内容</strong>
            <span>成为第一个分享经验的人。</span>
          </div>
        ) : null}
      </div>
      {!hasMore && items.length > 0 ? <p className={styles.end}>已经看到全部内容</p> : null}
      </AppLayout>
      <button
        className={styles.assistantTrigger}
        type="button"
        onClick={() => setAssistantOpen(true)}
        aria-label="打开 GREEN-BOOK 助手"
        aria-haspopup="dialog"
      >
        <span><AssistantIcon width={23} height={23} aria-hidden="true" /></span>
        <span className={styles.assistantLabel}>
          <strong>GREEN-BOOK 助手</strong>
          <small>帮你找、写、定时发布</small>
        </span>
      </button>
      <AssistantPanel open={assistantOpen} onClose={() => setAssistantOpen(false)} />
    </>
  );
};

export default HomePage;
