export const NOTIFICATION_UNREAD_CHANGED = "zhiguang:notification-unread-changed";

export const emitNotificationUnreadChanged = (unreadCount?: number) => {
  window.dispatchEvent(new CustomEvent(NOTIFICATION_UNREAD_CHANGED, { detail: { unreadCount } }));
};
