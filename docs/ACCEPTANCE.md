# GreenBook 联调验收

## 自动化检查

提交前运行：

```powershell
.\scripts\verify-all.ps1
```

该命令检查 Compose 配置、Java 测试、前端类型与生产构建，以及三个 Agent 的测试。

应用全部启动后先检查真实服务：

```powershell
.\scripts\e2e-test.ps1 -HealthOnly
```

使用专用 `USER` 测试账号执行 Java JWT → Creator → Assistant 直接回答链路：

```dotenv
GREENBOOK_E2E_IDENTIFIER_TYPE=EMAIL
GREENBOOK_E2E_IDENTIFIER=<专用测试账号>
GREENBOOK_E2E_PASSWORD=<测试账号密码>
```

```powershell
.\scripts\e2e-test.ps1 -Scenario Direct
```

验证 Assistant → Creator → Java 草稿交接：

```powershell
.\scripts\e2e-test.ps1 -Scenario CreatorDraft
```

CreatorDraft 场景只删除本次运行创建的测试草稿；删除失败时会输出草稿 ID，供手工清理。
脚本不会打印密码、access token 或服务密钥。

运行行为评测：

```powershell
uv run pytest -q tests/unit/test_agent_evaluation_runtime.py tests/evaluation
```

## 人工产品验收

- [ ] `/create` 同时显示“自己创作”和“AI 创作”。
- [ ] AI 创作使用当前 Java 用户身份进入 Creator Studio。
- [ ] Creator 完成后返回 Java `AI_ASSISTED` 草稿，并进入同一补图和发布向导。
- [ ] AI 草稿标题、摘要、正文和内容指纹与 Creator 最终版本一致。
- [ ] AI 草稿被编辑后，旧人工确认或定时发布授权失效。
- [ ] 手动创作上传正文和图片后可以预览，刷新后图片仍然存在。
- [ ] 手动创作发布后页面正确展示 `published`、`rejected` 或 `deleted`。
- [ ] 主页一次加载20条帖子，滚动到底后继续加载下一页。
- [ ] Assistant 对话框可使用键盘打开、关闭和循环焦点。
- [ ] 简单问题使用 DIRECT，单只读工具使用 TOOL，创作任务使用 CREATOR。
- [ ] 复杂任务展示动态步骤、依赖、审批、失败原因和重试入口。
- [ ] 评论区多次 `@助手` 都能执行，结果 Markdown 正常渲染。
- [ ] 创建定时发布后重启 Assistant，任务不会丢失或重复创建草稿。
- [ ] 暂停和恢复任务后，已完成步骤不会再次产生副作用。
- [ ] 取消定时发布会撤销 Java Capability。
- [ ] “删除我的所有帖子”必须先枚举本人资源并等待一次人工确认。

## 服务地址

| 服务 | 地址 |
| --- | --- |
| Java | `http://127.0.0.1:8080/actuator/health` |
| Creator | `http://127.0.0.1:8092/actuator/health/ready` |
| Assistant API | `http://127.0.0.1:8094/health` |
| Frontend | `http://127.0.0.1:5173` |
