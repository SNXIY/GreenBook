import { Link } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import MainHeader from "@/components/layout/MainHeader";
import AuthStatus from "@/features/auth/AuthStatus";
import {
  ArrowRightIcon,
  CreateIcon,
  ShieldIcon,
  SparkIcon
} from "@/components/icons/Icon";
import styles from "./CreateHubPage.module.css";

const CreateHubPage = () => {
  return (
    <AppLayout
      header={
        <MainHeader
          headline="创作"
          subtitle="从一个真实想法开始，选择适合你的表达方式。"
          rightSlot={<AuthStatus />}
        />
      }
    >
      <div className={styles.hub}>
        <Link to="/create/manual" className={`${styles.card} ${styles.manualCard}`}>
          <span className={styles.icon}><CreateIcon aria-hidden="true" /></span>
          <span className={styles.eyebrow}>自主表达</span>
          <h2>自己写</h2>
          <p>按步骤完成正文、配图和可见范围，最终内容会进入 GreenBook Agent 发布流程。</p>
          <span className={styles.cardAction}>开始写作 <ArrowRightIcon aria-hidden="true" /></span>
        </Link>
        <Link to="/?assistant=1" className={`${styles.card} ${styles.aiCard}`}>
          <span className={styles.icon}><SparkIcon aria-hidden="true" /></span>
          <span className={styles.eyebrow}>AI 协作</span>
          <h2>和 AI 一起写</h2>
          <p>在首页打开 GreenBook Agent，直接对话生成草稿、修改并定时发布。</p>
          <span className={styles.cardAction}>打开 Agent <ArrowRightIcon aria-hidden="true" /></span>
        </Link>
      </div>
      <section className={styles.flow}>
        <div className={styles.flowTitle}>
          <ShieldIcon aria-hidden="true" />
          <div>
            <h3>两条路径，一套正式发布流程</h3>
            <p>自己写或交给 Agent 生成，发布前都会经过确认。</p>
          </div>
        </div>
        <ol>
          <li><span>1</span><strong>完成内容</strong><small>自己写或 AI 协作</small></li>
          <li><span>2</span><strong>确认设置</strong><small>预览与可见范围</small></li>
          <li><span>3</span><strong>正式发布</strong><small>确认后进入可靠执行流程</small></li>
        </ol>
      </section>
    </AppLayout>
  );
};

export default CreateHubPage;
