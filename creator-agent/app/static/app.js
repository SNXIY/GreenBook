const AUTH_KEY = "mindflow.creator.auth";
const AUTO_LOGIN_SUPPRESS_KEY = "mindflow.creator.auto-login-suppressed";

const els = {
  serviceState: document.querySelector("#serviceState"),
  loginForm: document.querySelector("#loginForm"),
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  loginState: document.querySelector("#loginState")
};

function setServiceState(text, tone) {
  els.serviceState.textContent = text;
  els.serviceState.className = `service-state ${tone}`;
}

function encodeBasicCredentials(username, password) {
  const bytes = new TextEncoder().encode(`${username}:${password}`);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

async function creatorStatus(token) {
  const response = await fetch("/api/v1/creator/status", {
    headers: { Authorization: `Basic ${token}` }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error?.message || "用户名或密码错误");
  }
  return response.json();
}

async function localSession() {
  const response = await fetch("/api/v1/creator/local-session", {
    method: "POST",
    cache: "no-store"
  });
  if (response.status === 403 || response.status === 404) return null;
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(body?.error?.message || "本地自动登录暂不可用");
  }
  return response.json();
}

function saveAuth(token, status) {
  sessionStorage.setItem(AUTH_KEY, JSON.stringify({
    token,
    creatorId: status.creator_id,
    tenantId: status.tenant_id,
    displayName: status.display_name
  }));
}

async function checkHealth() {
  try {
    const response = await fetch("/actuator/health/ready");
    const body = await response.json();
    setServiceState(
      body.status === "UP" ? "服务就绪" : "服务不可用",
      body.status === "UP" ? "is-ready" : "is-error"
    );
  } catch {
    setServiceState("服务不可用", "is-error");
  }
}

async function resumeLogin() {
  let auth = null;
  try {
    auth = JSON.parse(sessionStorage.getItem(AUTH_KEY) || "null");
  } catch {
    sessionStorage.removeItem(AUTH_KEY);
  }
  if (auth?.token) {
    try {
      saveAuth(auth.token, await creatorStatus(auth.token));
      window.location.replace("/creator.html");
      return;
    } catch {
      sessionStorage.removeItem(AUTH_KEY);
    }
  }
  if (sessionStorage.getItem(AUTO_LOGIN_SUPPRESS_KEY) === "true") return;
  els.loginState.textContent = "正在进入本地创作工作台...";
  try {
    const session = await localSession();
    if (!session) {
      els.loginState.textContent = "请输入本地开发账号";
      return;
    }
    saveAuth(session.token, session);
    window.location.replace("/creator.html");
  } catch (error) {
    els.loginState.textContent = `自动登录失败：${error.message}`;
  }
}

async function login(event) {
  event.preventDefault();
  sessionStorage.removeItem(AUTO_LOGIN_SUPPRESS_KEY);
  const token = encodeBasicCredentials(
    els.username.value.trim(),
    els.password.value
  );
  els.loginState.textContent = "正在验证身份...";
  try {
    const status = await creatorStatus(token);
    saveAuth(token, status);
    els.loginState.textContent = "登录成功";
    window.location.assign("/creator.html");
  } catch (error) {
    sessionStorage.removeItem(AUTH_KEY);
    els.loginState.textContent = `登录失败：${error.message}`;
  }
}

els.loginForm.addEventListener("submit", login);
checkHealth();
resumeLogin();
