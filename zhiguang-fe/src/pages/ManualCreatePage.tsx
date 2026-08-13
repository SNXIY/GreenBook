import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import AppLayout from "@/components/layout/AppLayout";
import MainHeader from "@/components/layout/MainHeader";
import TagInput from "@/components/common/TagInput";
import {
  AlertIcon,
  ArrowLeftIcon,
  ArrowRightIcon,
  CheckIcon,
  ShieldIcon
} from "@/components/icons/Icon";
import AuthStatus from "@/features/auth/AuthStatus";
import { useAuth } from "@/context/AuthContext";
import { computeSha256, knowpostService, uploadToPresigned } from "@/services/knowpostService";
import styles from "./CreatePage.module.css";

type ContentOrigin = "MANUAL" | "AI_ASSISTED";

const steps = [
  { title: "内容", description: "标题与正文" },
  { title: "配图", description: "图片与标签" },
  { title: "设置", description: "可见范围" },
  { title: "发布", description: "确认并提交" }
];

const MAX_IMAGES = 15;
const MAX_IMAGE_SIZE = 5 * 1024 * 1024;
const LOCAL_DRAFT_KEY = "zhiguang_manual_create_autosave";

const normalizeTitle = (value: string) =>
  value.replace(/^\s{0,3}#{1,6}\s+/, "").trim().replace(/^[*_`]+|[*_`]+$/g, "");

const stripDuplicateLeadingTitle = (body: string, title: string) => {
  const lines = body.replace(/\r\n?/g, "\n").replace(/^\uFEFF/, "").split("\n");
  while (lines.length && !lines[0].trim()) lines.shift();
  const expected = normalizeTitle(title);
  while (lines.length && normalizeTitle(lines[0]) === expected) {
    lines.shift();
    while (lines.length && !lines[0].trim()) lines.shift();
  }
  return lines.join("\n").trim();
};

const plainExcerpt = (value: string) =>
  value
    .replace(/!\[[^\]]*]\([^)]*\)/g, "")
    .replace(/\[([^\]]+)]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}(?:#{1,6}|>|[-*+])\s+/gm, "")
    .replace(/[`*_~]/g, "")
    .replace(/\s+/g, " ")
    .trim();

const deriveSummary = (body: string, title: string) => {
  for (const paragraph of body.split(/\n\s*\n/)) {
    const candidate = plainExcerpt(paragraph);
    if (candidate && candidate !== plainExcerpt(title)) return candidate.slice(0, 50);
  }
  return "";
};

const ManualCreatePage = () => {
  const { tokens } = useAuth();
  const [searchParams] = useSearchParams();
  const [step, setStep] = useState(0);
  const [tags, setTags] = useState<string[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [visiblePublic, setVisiblePublic] = useState(true);
  const [summary, setSummary] = useState("");
  const [aiSummaryLoading, setAiSummaryLoading] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [postId, setPostId] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [contentOrigin, setContentOrigin] = useState<ContentOrigin>("MANUAL");
  const [published, setPublished] = useState(false);
  const [publishState, setPublishState] = useState<"idle" | "uploading" | "done">("idle");
  const [uploadedImgUrls, setUploadedImgUrls] = useState<string[]>([]);
  const [imageUploading, setImageUploading] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const localDraftReady = useRef(false);

  useEffect(() => {
    const draftId = searchParams.get("draftId");
    if (draftId || typeof window === "undefined") {
      localDraftReady.current = true;
      return;
    }
    try {
      const raw = localStorage.getItem(LOCAL_DRAFT_KEY);
      if (raw) {
        const draft = JSON.parse(raw) as {
          title?: string;
          content?: string;
          summary?: string;
          tags?: string[];
          visiblePublic?: boolean;
        };
        setTitle(draft.title || "");
        setContent(draft.content || "");
        setSummary(draft.summary || "");
        setTags(Array.isArray(draft.tags) ? draft.tags : []);
        setVisiblePublic(draft.visiblePublic !== false);
        if (draft.title || draft.content) {
          setMessage("已恢复上次未完成的本地草稿");
        }
      }
    } catch {
      localStorage.removeItem(LOCAL_DRAFT_KEY);
    } finally {
      localDraftReady.current = true;
    }
  }, [searchParams]);

  useEffect(() => {
    if (!localDraftReady.current || contentOrigin !== "MANUAL" || published) return;
    const timer = window.setTimeout(() => {
      localStorage.setItem(
        LOCAL_DRAFT_KEY,
        JSON.stringify({ title, content, summary, tags, visiblePublic })
      );
    }, 500);
    return () => window.clearTimeout(timer);
  }, [content, contentOrigin, published, summary, tags, title, visiblePublic]);

  useEffect(() => {
    const draftId = searchParams.get("draftId");
    if (!draftId || !tokens?.accessToken) return;
    let cancelled = false;
    void (async () => {
      setError(null);
      try {
        const detail = await knowpostService.detail(draftId, tokens.accessToken);
        if (cancelled) return;
        setPostId(String(detail.id));
        const loadedTitle = normalizeTitle(detail.title || "");
        setTitle(loadedTitle);
        setTags(detail.tags || []);
        setUploadedImgUrls(detail.images || []);
        const origin: ContentOrigin =
          detail.contentOrigin === "AI_ASSISTED" ? "AI_ASSISTED" : "MANUAL";
        setContentOrigin(origin);
        const rawContent = await knowpostService.content(draftId, tokens.accessToken);
        const loadedContent = stripDuplicateLeadingTitle(rawContent, loadedTitle);
        if (cancelled) return;
        setContent(loadedContent);
        const storedSummary = plainExcerpt(detail.description || "");
        const invalidStoredSummary =
          !storedSummary ||
          storedSummary === plainExcerpt(loadedTitle) ||
          (detail.description || "").trimStart().startsWith("#");
        setSummary(
          invalidStoredSummary
            ? deriveSummary(loadedContent, loadedTitle)
            : storedSummary.slice(0, 50)
        );
        setMessage(
          origin === "AI_ASSISTED"
            ? "AI 成稿已回填。请补充配图与发布设置。"
            : "草稿已加载，可以继续编辑。"
        );
      } catch (cause) {
        if (!cancelled) {
          setError(cause instanceof Error ? cause.message : "草稿加载失败");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [searchParams, tokens?.accessToken]);

  const ensureDraft = async () => {
    if (postId) return postId;
    const response = await knowpostService.createDraft();
    const id = String(response.id);
    setPostId(id);
    return id;
  };

  const validateContentStep = () => {
    if (!title.trim()) {
      setError("请先填写标题");
      return false;
    }
    if (!content.trim()) {
      setError("请先填写正文");
      return false;
    }
    if (summary.trim().length > 50) {
      setError("摘要不能超过 50 字");
      return false;
    }
    return true;
  };

  const goNext = () => {
    setError(null);
    if (step === 0 && !validateContentStep()) return;
    setStep(current => Math.min(steps.length - 1, current + 1));
    window.scrollTo({ top: 0, behavior: "auto" });
  };

  const goBack = () => {
    setError(null);
    setStep(current => Math.max(0, current - 1));
    window.scrollTo({ top: 0, behavior: "auto" });
  };

  const handleSelectImages = async (files: FileList | null) => {
    if (!files?.length) return;
    setError(null);
    setImageUploading(true);
    try {
      const candidates = Array.from(files);
      const invalid = candidates.find(
        file =>
          !["image/jpeg", "image/png", "image/webp"].includes(file.type) ||
          file.size > MAX_IMAGE_SIZE
      );
      if (invalid) {
        throw new Error("仅支持 JPG、PNG、WebP，且单张图片不能超过 5MB");
      }

      const remaining = MAX_IMAGES - uploadedImgUrls.length;
      if (remaining <= 0) throw new Error(`最多上传 ${MAX_IMAGES} 张图片`);
      const selectedFiles = candidates.slice(0, remaining);
      const id = await ensureDraft();
      const nextUrls: string[] = [];

      for (const file of selectedFiles) {
        const extension = file.name.match(/\.[^.]+$/)?.[0] || ".jpg";
        const presign = await knowpostService.presign({
          scene: "knowpost_image",
          postId: id,
          contentType: file.type,
          ext: extension
        });
        await uploadToPresigned(presign.putUrl, presign.headers, file);
        if (!presign.publicUrl?.trim()) {
          throw new Error("上传服务没有返回图片访问地址");
        }
        nextUrls.push(presign.publicUrl);
      }

      setUploadedImgUrls(current => [...current, ...nextUrls]);
      setMessage(`${nextUrls.length} 张图片已上传到内容存储`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "图片上传失败");
    } finally {
      setImageUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const generateSummary = async () => {
    if (!tokens?.accessToken) {
      setError("请先登录后使用 AI 摘要");
      return;
    }
    if (!content.trim()) {
      setError("请先填写正文");
      return;
    }
    setAiSummaryLoading(true);
    setError(null);
    try {
      const response = await knowpostService.suggestDescription(content, tokens.accessToken);
      setSummary((response.description || "").slice(0, 50));
      setMessage("摘要已根据正文生成");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "摘要生成失败");
    } finally {
      setAiSummaryLoading(false);
    }
  };

  const handlePublish = async () => {
    if (!validateContentStep() || submitting) return;
    setSubmitting(true);
    setPublished(false);
    setError(null);
    setMessage(null);
    setPublishState("uploading");
    try {
      const id = await ensureDraft();
      const contentFile = new File([content], "content.md", { type: "text/markdown" });
      const sha256 = await computeSha256(contentFile);
      const presign = await knowpostService.presign({
        scene: "knowpost_content",
        postId: id,
        contentType: "text/markdown",
        ext: ".md"
      });
      const { etag } = await uploadToPresigned(
        presign.putUrl,
        presign.headers,
        contentFile
      );
      if (!etag.trim()) {
        throw new Error(
          "正文已经上传，但对象存储没有返回 ETag。请检查上传服务的 CORS Expose-Headers 配置。"
        );
      }

      await knowpostService.confirmContent(id, {
        objectKey: presign.objectKey,
        etag,
        size: contentFile.size,
        sha256
      });
      await knowpostService.update(id, {
        title: title.trim(),
        tags: tags.length ? tags : undefined,
        imgUrls: uploadedImgUrls.length ? uploadedImgUrls : undefined,
        visible: visiblePublic ? "public" : "private",
        isTop: false,
        description: summary.trim() || undefined
      });

      const result = await knowpostService.publish(id);
      if (result.status !== "published") {
        throw new Error("发布未完成，请稍后查看任务状态");
      }

      setPublishState("done");
      setPublished(true);
      setMessage(
        contentOrigin === "AI_ASSISTED"
          ? "AI 成稿已通过 Java 发布流程正式发布。"
          : "内容已正式发布。"
      );
      localStorage.removeItem(LOCAL_DRAFT_KEY);
    } catch (cause) {
      setPublishState("idle");
      setError(cause instanceof Error ? cause.message : "发布失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  };

  const renderStep = () => {
    if (step === 0) {
      return (
        <section className={styles.stepPanel} aria-labelledby="content-step-title">
          <div className={styles.sectionHeading}>
            <span>01</span>
            <div>
              <h2 id="content-step-title">先把内容写清楚</h2>
              <p>AI 成稿也会回到这里，最终文字始终由你确认。</p>
            </div>
          </div>
          <div className={styles.field}>
            <label htmlFor="title">标题 <span aria-hidden="true">*</span></label>
            <input
              id="title"
              name="title"
              className={styles.input}
              value={title}
              onChange={event => setTitle(event.target.value)}
              placeholder="例如：我如何建立每周阅读习惯…"
              autoComplete="off"
              maxLength={256}
            />
          </div>
          <div className={styles.field}>
            <label htmlFor="content">正文 <span aria-hidden="true">*</span></label>
            <textarea
              id="content"
              name="content"
              className={styles.editor}
              value={content}
              onChange={event => setContent(event.target.value)}
              placeholder="写下你的观察、方法与经验…"
              autoComplete="off"
            />
            <small>{content.trim().length.toLocaleString("zh-CN")} 字符 · 支持 Markdown</small>
          </div>
          <div className={styles.field}>
            <div className={styles.fieldHeader}>
              <label htmlFor="summary">摘要</label>
              <button
                type="button"
                className={styles.summaryButton}
                onClick={() => void generateSummary()}
                disabled={aiSummaryLoading}
              >
                {aiSummaryLoading ? "生成中…" : "用 AI 提炼"}
              </button>
            </div>
            <textarea
              id="summary"
              name="summary"
              className={styles.summary}
              value={summary}
              onChange={event => setSummary(event.target.value)}
              placeholder="例如：3 个可以立即开始的阅读方法…"
              autoComplete="off"
              maxLength={50}
            />
            <small className={styles.charCount}>{summary.length} / 50</small>
          </div>
        </section>
      );
    }

    if (step === 1) {
      return (
        <section className={styles.stepPanel} aria-labelledby="media-step-title">
          <div className={styles.sectionHeading}>
            <span>02</span>
            <div>
              <h2 id="media-step-title">补充图片与标签</h2>
              <p>图片会先通过 Java 获取 OSS 签名，再由浏览器直传存储。</p>
            </div>
          </div>
          <div className={styles.field}>
            <label>内容图片</label>
            <button
              type="button"
              className={styles.uploadBox}
              onClick={() => fileInputRef.current?.click()}
              disabled={imageUploading || uploadedImgUrls.length >= MAX_IMAGES}
            >
              <strong>{imageUploading ? "正在上传…" : "选择图片"}</strong>
              <span>JPG、PNG、WebP · 单张不超过 5MB · 最多 {MAX_IMAGES} 张</span>
            </button>
            <input
              ref={fileInputRef}
              id="publish-images"
              name="images"
              aria-label="选择内容图片"
              type="file"
              accept="image/jpeg,image/png,image/webp"
              multiple
              className={styles.fileInputHidden}
              onChange={event => void handleSelectImages(event.target.files)}
            />
            {uploadedImgUrls.length ? (
              <div className={styles.thumbGrid}>
                {uploadedImgUrls.map((url, index) => (
                  <div className={styles.thumbItem} key={url}>
                    <button type="button" onClick={() => setPreviewUrl(url)}>
                      <img src={url} alt={`内容图片 ${index + 1}`} width="640" height="480" loading="lazy" />
                    </button>
                    <button
                      type="button"
                      className={styles.removeImage}
                      onClick={() =>
                        setUploadedImgUrls(current => current.filter(item => item !== url))
                      }
                      aria-label={`移除第 ${index + 1} 张图片`}
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <p className={styles.helper}>没有图片也可以继续发布纯文字内容。</p>
            )}
          </div>
          <div className={styles.field}>
            <label htmlFor="tags">标签</label>
            <TagInput
              id="tags"
              value={tags}
              onChange={setTags}
              placeholder="输入标签后按回车"
            />
            <small>建议添加 1–5 个标签，帮助内容被合适的读者发现。</small>
          </div>
        </section>
      );
    }

    if (step === 2) {
      return (
        <section className={styles.stepPanel} aria-labelledby="settings-step-title">
          <div className={styles.sectionHeading}>
            <span>03</span>
            <div>
              <h2 id="settings-step-title">决定谁能看到</h2>
              <p>只展示当前后端真正支持的发布设置。</p>
            </div>
          </div>
          <fieldset className={styles.visibility}>
            <legend>可见范围</legend>
            <label className={visiblePublic ? styles.choiceActive : styles.choice}>
              <input
                type="radio"
                name="visibility"
                checked={visiblePublic}
                onChange={() => setVisiblePublic(true)}
              />
              <span>
                <strong>公开</strong>
                <small>发布后会出现在社区内容流中</small>
              </span>
              {visiblePublic ? <CheckIcon aria-hidden="true" /> : null}
            </label>
            <label className={!visiblePublic ? styles.choiceActive : styles.choice}>
              <input
                type="radio"
                name="visibility"
                checked={!visiblePublic}
                onChange={() => setVisiblePublic(false)}
              />
              <span>
                <strong>私密</strong>
                <small>仅自己可以查看</small>
              </span>
              {!visiblePublic ? <CheckIcon aria-hidden="true" /> : null}
            </label>
          </fieldset>
          <article className={styles.previewCard}>
            <span>发布预览</span>
            {uploadedImgUrls[0] ? (
              <img src={uploadedImgUrls[0]} alt="" width="640" height="480" />
            ) : (
              <div className={styles.previewPlaceholder}>GreenBook</div>
            )}
            <div>
              <h3>{title || "还没有标题"}</h3>
              <p>{summary || content.slice(0, 80) || "内容摘要会显示在这里"}</p>
              <small>{tags.length ? tags.map(tag => `#${tag}`).join("  ") : "尚未添加标签"}</small>
            </div>
          </article>
        </section>
      );
    }

    return (
      <section className={styles.stepPanel} aria-labelledby="publish-step-title">
        <div className={styles.sectionHeading}>
          <span>04</span>
          <div>
            <h2 id="publish-step-title">最后确认</h2>
            <p>提交的是上一步预览过的最终内容。</p>
          </div>
        </div>
        <article className={styles.publicationCard}>
          <span className={styles.publicationIcon}><ShieldIcon aria-hidden="true" /></span>
          <div>
            <h3>
              {contentOrigin === "AI_ASSISTED" ? "AI 成稿直接发布" : "确认发布"}
            </h3>
            <p>
              {contentOrigin === "AI_ASSISTED"
                ? "来源由Creator Service 与 Java 服务间签名确认。你补充的图片和设置会一起进入正式发布流程。"
                : "标题、摘要、标签和正文会按当前发布策略校验，确认后进入可靠发布流程。"}
            </p>
          </div>
        </article>
        <dl className={styles.summaryList}>
          <div><dt>标题</dt><dd>{title}</dd></div>
          <div><dt>图片</dt><dd>{uploadedImgUrls.length} 张</dd></div>
          <div><dt>标签</dt><dd>{tags.length ? tags.join("、") : "未添加"}</dd></div>
          <div><dt>可见范围</dt><dd>{visiblePublic ? "公开" : "私密"}</dd></div>
        </dl>
        {publishState !== "idle" ? (
          <div className={styles.progress} role="status" aria-live="polite">
            <span className={publishState === "done" ? styles.progressDone : styles.spinner} />
            <div>
              <strong>
                {publishState === "uploading"
                  ? "正在写入内容存储"
                  : "发布完成"}
              </strong>
              <small>请勿重复提交，当前页面会自动更新结果。</small>
            </div>
          </div>
        ) : null}
        <button
          type="button"
          className={styles.publish}
          onClick={() => void handlePublish()}
          disabled={submitting || published}
        >
          {submitting
            ? "正在处理…"
            : published
              ? "已发布"
              : contentOrigin === "AI_ASSISTED"
                ? "确认发布"
                : "确认发布"}
        </button>
        {published && postId ? (
          <Link className={styles.viewPost} to={`/post/${postId}`}>查看已发布内容</Link>
        ) : null}
        <details className={styles.technical}>
          <summary>技术信息</summary>
          <p>草稿 ID：{postId || "将在首次上传时创建"}</p>
          <p>正文与图片：通过 Java 预签名接口写入 {import.meta.env.PROD ? "OSS" : "当前配置的对象存储"}</p>
        </details>
      </section>
    );
  };

  return (
    <AppLayout
      header={
        <MainHeader
          headline={contentOrigin === "AI_ASSISTED" ? "完善 AI 成稿" : "写一篇知文"}
          subtitle="内容会自动保存到本机；发布前再统一写入对象存储。"
          rightSlot={
            <div className={styles.headerActions}>
              <Link to="/create" className={styles.backLink}>创作方式</Link>
              <AuthStatus />
            </div>
          }
        />
      }
    >
      <nav className={styles.stepper} aria-label="发布进度">
        {steps.map((item, index) => (
          <button
            type="button"
            key={item.title}
            className={
              index === step
                ? styles.stepActive
                : index < step
                  ? styles.stepDone
                  : styles.step
            }
            onClick={() => {
              if (index <= step || validateContentStep()) setStep(index);
            }}
            aria-current={index === step ? "step" : undefined}
          >
            <span>{index < step ? <CheckIcon aria-hidden="true" /> : index + 1}</span>
            <strong>{item.title}</strong>
            <small>{item.description}</small>
          </button>
        ))}
      </nav>

      <div className={styles.formCard}>
        {contentOrigin === "AI_ASSISTED" ? (
          <div className={styles.originBanner}>
            <CheckIcon aria-hidden="true" />
            <span><strong>AI 成稿已安全回填</strong>来源身份由服务端锁定，普通请求无法伪造。</span>
          </div>
        ) : null}
        {renderStep()}

        {error ? (
          <div className={styles.error} role="alert">
            <AlertIcon aria-hidden="true" />
            <span>{error}</span>
          </div>
        ) : null}
        {message ? <div className={styles.success} role="status">{message}</div> : null}

        <div className={styles.actions}>
          {step > 0 ? (
            <button type="button" className={styles.secondary} onClick={goBack} disabled={submitting}>
              <ArrowLeftIcon aria-hidden="true" />上一步
            </button>
          ) : (
            <Link to="/create" className={styles.secondary}>
              <ArrowLeftIcon aria-hidden="true" />返回
            </Link>
          )}
          {step < steps.length - 1 ? (
            <button type="button" className={styles.primary} onClick={goNext}>
              下一步<ArrowRightIcon aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>

      {previewUrl ? (
        <div className={styles.previewOverlay} role="dialog" aria-modal="true" aria-label="图片预览">
          <button type="button" onClick={() => setPreviewUrl(null)} aria-label="关闭图片预览">
            关闭
          </button>
          <img src={previewUrl} className={styles.previewImage} alt="内容图片预览" width="1200" height="900" />
        </div>
      ) : null}
    </AppLayout>
  );
};

export default ManualCreatePage;
