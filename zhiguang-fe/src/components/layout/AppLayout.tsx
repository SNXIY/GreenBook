import type { ReactNode } from "react";
import Sidebar from "./Sidebar";
import styles from "./AppLayout.module.css";

type AppLayoutProps = {
  header?: ReactNode;
  children: ReactNode;
  variant?: "default" | "cardless";
};

const AppLayout = ({ header, children, variant = "default" }: AppLayoutProps) => {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">跳到主要内容</a>
      <Sidebar />
      <div className={styles.container}>
        {header}
        <main id="main-content" className={variant === "default" ? styles.pageCard : styles.main}>
          {children}
        </main>
      </div>
    </div>
  );
};

export default AppLayout;
