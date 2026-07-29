export type NotificationItem = {
  id: string;
  type: "LIKE" | "FAVORITE" | "COMMENT" | "REPLY" | "FOLLOW" | string;
  title: string;
  content?: string;
  targetType: string;
  targetId: string;
  aggregateType?: string;
  aggregateId?: string;
  actorId?: string;
  latestActorId?: string;
  actorName?: string;
  actorAvatar?: string;
  actorCount: number;
  read: boolean;
  createTime: string;
};

export type NotificationPageResponse = {
  items: NotificationItem[];
  nextCursor?: string | null;
  hasMore: boolean;
};

export type UnreadCountResponse = {
  unreadCount: number;
};
