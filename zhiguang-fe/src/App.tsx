import { Navigate, Route, Routes } from "react-router-dom";
import HomePage from "./pages/HomePage";
import CreateHubPage from "./pages/CreateHubPage";
import ManualCreatePage from "./pages/ManualCreatePage";
import AiCreatePage from "./pages/AiCreatePage";
import TaskCenterPage from "./pages/TaskCenterPage";
import ProfilePage from "./pages/ProfilePage";
import EditProfilePage from "./pages/EditProfilePage";
import CourseDetailPage from "./pages/CourseDetailPage";
import LoginPage from "./pages/LoginPage";
import RegisterPage from "./pages/RegisterPage";
import NotificationsPage from "./pages/NotificationsPage";
import AdminModerationPage from "./pages/AdminModerationPage";
import { useAuth } from "./context/AuthContext";

function App() {
  const { user, isLoading } = useAuth();

  if (!isLoading && user?.role === "ADMIN") {
    return (
      <Routes>
        <Route path="/admin/moderation" element={<AdminModerationPage />} />
        <Route path="*" element={<Navigate to="/admin/moderation" replace />} />
      </Routes>
    );
  }

  return (
    <Routes>
      <Route path="/" element={<HomePage />} />
      <Route path="/create" element={<CreateHubPage />} />
      <Route path="/create/manual" element={<ManualCreatePage />} />
      <Route path="/create/ai" element={<AiCreatePage />} />
      <Route path="/tasks" element={<TaskCenterPage />} />
      <Route path="/profile" element={<ProfilePage />} />
      <Route path="/profile/edit" element={<EditProfilePage />} />
      <Route path="/post/:id" element={<CourseDetailPage />} />
      <Route path="/notifications" element={<NotificationsPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      <Route path="/admin/*" element={<Navigate to="/" replace />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default App;
