export type PostTaskItem = {
  id: string;
  title?: string | null;
  status: "draft" | "published" | "rejected" | "deleted";
  contentOrigin?: "MANUAL" | "AI_ASSISTED" | null;
  createdAt: string;
  updatedAt: string;
  publishedAt?: string | null;
};
