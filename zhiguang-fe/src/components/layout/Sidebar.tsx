import { NavLink } from "react-router-dom";
import { useEffect, useState } from "react";
import { BellIcon, CreateIcon, HomeIcon, LeafIcon, ProfileIcon, TaskIcon } from "@/components/icons/Icon";
import { useAuth } from "@/context/AuthContext";
import { NOTIFICATION_UNREAD_CHANGED } from "@/services/notificationEvents";
import { notificationService } from "@/services/notificationService";
import styles from "./Sidebar.module.css";

const navItems = [
  { to: "/", label: "首页", Icon: HomeIcon },
  { to: "/create", label: "创作", Icon: CreateIcon },
  { to: "/tasks", label: "任务", Icon: TaskIcon },
  { to: "/notifications", label: "消息", Icon: BellIcon },
  { to: "/profile", label: "我的", Icon: ProfileIcon }
] as const;

const Sidebar = () => {
  const { tokens } = useAuth();
  const [unreadCount, setUnreadCount] = useState(0);

  useEffect(() => {
    if (!tokens?.accessToken) {
      setUnreadCount(0);
      return;
    }

    let active = true;
    const loadUnreadCount = async () => {
      try {
        const resp = await notificationService.unreadCount();
        if (active) {
          setUnreadCount(resp.unreadCount ?? 0);
        }
      } catch {
        if (active) {
          setUnreadCount(0);
        }
      }
    };

    void loadUnreadCount();
    const timer = window.setInterval(loadUnreadCount, 30_000);
    const handleUnreadChanged = (event: Event) => {
      const customEvent = event as CustomEvent<{ unreadCount?: number }>;
      const nextCount = customEvent.detail?.unreadCount;
      if (typeof nextCount === "number") {
        setUnreadCount(Math.max(0, nextCount));
        return;
      }
      void loadUnreadCount();
    };
    window.addEventListener("focus", loadUnreadCount);
    window.addEventListener(NOTIFICATION_UNREAD_CHANGED, handleUnreadChanged);

    return () => {
      active = false;
      window.clearInterval(timer);
      window.removeEventListener("focus", loadUnreadCount);
      window.removeEventListener(NOTIFICATION_UNREAD_CHANGED, handleUnreadChanged);
    };
  }, [tokens?.accessToken]);

  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <div className={styles.logo}>
          <LeafIcon width={25} height={25} aria-hidden="true" />
        </div>
        <div className={styles.brandText}>
          <strong>GREEN-BOOK</strong>
          <span>Knowledge grows here</span>
        </div>
      </div>
      <nav className={styles.nav}>
        {navItems.map(({ to, label, Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === "/"}
            className={({ isActive }) => (isActive ? `${styles.link} ${styles.linkActive}` : styles.link)}
          >
            <span className={styles.iconWrap}>
              <Icon aria-hidden="true" />
              {to === "/notifications" && unreadCount > 0 ? (
                <span className={styles.badge}>{unreadCount > 99 ? "99+" : unreadCount}</span>
              ) : null}
            </span>
            {label}
          </NavLink>
        ))}
      </nav>
      <div className={styles.divider} />
      <div className={styles.footer}>
        <span>GREEN-BOOK</span>
        <div>让知识自然生长</div>
      </div>
    </aside>
  );
};

export default Sidebar;
