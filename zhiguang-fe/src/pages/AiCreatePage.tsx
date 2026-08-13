import { useEffect, useMemo } from "react";
import { Link, Navigate, useNavigate } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import MainHeader from "@/components/layout/MainHeader";
import AuthStatus from "@/features/auth/AuthStatus";
import { useAuth } from "@/context/AuthContext";
import { CheckIcon, ShieldIcon } from "@/components/icons/Icon";
import styles from "./AiCreatePage.module.css";

const studioUrl =
  (import.meta.env.VITE_CREATOR_STUDIO_URL as string | undefined)?.replace(/\/$/, "") ||
  "http://127.0.0.1:8092/creator.html";

const AiCreatePage = () => {
  const { tokens, isLoading } = useAuth();
  const navigate = useNavigate();
  const studioOrigin = useMemo(() => {
    try {
      return new URL(studioUrl, window.location.href).origin;
    } catch {
      return window.location.origin;
    }
  }, []);

  useEffect(() => {
    const onMessage = (event: MessageEvent) => {
      if (event.origin !== studioOrigin) return;
      const data = event.data as { type?: string; draftId?: string } | null;
      if (data?.type !== "greenbook.creator.handoff" || !data.draftId) return;
      navigate(`/create/manual?draftId=${encodeURIComponent(data.draftId)}`);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [navigate, studioOrigin]);

  if (!isLoading && !tokens?.accessToken) {
    return <Navigate to="/login" replace state={{ from: "/create/ai" }} />;
  }

  const frameSrc = tokens?.accessToken
    ? `${studioUrl}#zhiguang_token=${encodeURIComponent(tokens.accessToken)}`
    : studioUrl;

  return (
    <AppLayout
      header={
        <MainHeader
          headline="AI 创作"
          subtitle="从想法到成稿，关键节点由你确认；完成后回到 GreenBook 补图并正式发布。"
          rightSlot={
            <div className={styles.headerActions}>
              <Link to="/create" className={styles.backLink}>
                创作方式
              </Link>
              <AuthStatus />
            </div>
          }
        />
      }
    >
      <div className={styles.handoffGuide}>
        <span><CheckIcon aria-hidden="true" /></span>
        <div>
          <strong>成稿后点击“去 GreenBook 发布”</strong>
          <small>系统会自动创建 Java 草稿并打开同一套渐进发布向导。</small>
        </div>
        <details>
          <summary>连接说明</summary>
          <p>当前页面使用你的 GreenBook 登录身份连接Creator Service；服务地址与任务记录默认不向普通用户展开。</p>
        </details>
        <ShieldIcon className={styles.shield} aria-hidden="true" />
      </div>
      <div className={styles.frameWrap}>
        <iframe
          className={styles.frame}
          title="GreenBook AI 创作工作台"
          src={frameSrc}
          allow="clipboard-read; clipboard-write"
        />
      </div>
    </AppLayout>
  );
};

export default AiCreatePage;
