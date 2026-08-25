import { Navigate } from "react-router-dom";

/**
 * Compatibility route for old bookmarks. Runtime executions are not a
 * customer-facing workspace; business state lives in My Content and the
 * Agent conversation. Keep this route only so old links land there safely.
 */
const TaskCenterPage = () => <Navigate to="/profile#my-content" replace />;

export default TaskCenterPage;
