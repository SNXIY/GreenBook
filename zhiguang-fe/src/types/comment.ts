export type CommentItem = {
  id: string;
  postId: string;
  parentId?: string | null;
  rootId?: string | null;
  userId: string;
  authorNickname: string;
  authorAvatar?: string | null;
  content: string;
  top: boolean;
  replyCount: number;
  likeCount: number;
  liked: boolean;
  assistant: boolean;
  createTime: string;
};

export type CommentPageResponse = {
  items: CommentItem[];
  nextCursor?: string | null;
  hasMore: boolean;
};

export type CommentCreateRequest = {
  postId: string;
  parentId?: string;
  content: string;
};
