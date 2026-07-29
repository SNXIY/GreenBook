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
          <p>按步骤完成正文、配图和可见范围，最终内容会交给审核 Agent。</p>
          <span className={styles.cardAction}>开始写作 <ArrowRightIcon aria-hidden="true" /></span>
        </Link>
        <Link to="/create/ai" className={`${styles.card} ${styles.aiCard}`}>
          <span className={styles.icon}><SparkIcon aria-hidden="true" /></span>
          <span className={styles.eyebrow}>AI 协作</span>
          <h2>和 AI 一起写</h2>
          <p>由创作 Agent 研究、搭结构并生成初稿，再回到同一发布向导补图确认。</p>
          <span className={styles.cardAction}>打开创作助手 <ArrowRightIcon aria-hidden="true" /></span>
        </Link>
      </div>
      <section className={styles.flow}>
        <div className={styles.flowTitle}>
          <ShieldIcon aria-hidden="true" />
          <div>
            <h3>两条路径，一套正式发布流程</h3>
            <p>图片经 Java 预签名上传到对象存储，内容来源由服务端锁定。</p>
          </div>
        </div>
        <ol>
          <li><span>1</span><strong>完成内容</strong><small>自己写或 AI 协作</small></li>
          <li><span>2</span><strong>补充配图</strong><small>OSS / 本地对象存储</small></li>
          <li><span>3</span><strong>确认设置</strong><small>预览与可见范围</small></li>
          <li><span>4</span><strong>正式发布</strong><small>手写内容进入真实审核</small></li>
        </ol>
      </section>
    </AppLayout>
  );
};

export default CreateHubPage;
