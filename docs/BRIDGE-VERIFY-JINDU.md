# 原 DSH 会话接入验收（Issue #2 · 津渡）

- 日期：2026-10-21
- 宿主：Windows + DeepSeek Harness 桌面端，Python 3.14.7（要求 3.10+）
- 服务：`http://127.0.0.1:8787`，仅绑定回环地址
- 分支：`feat/dsh-bridge-verify`（基于 `feat/bridge-mvp-v01`，不改 main）

本文件记录的是**在原来的 DSH 会话里真实执行**的验证，不是模拟脚本。
账号与 token 只存在于本机，未写入仓库、未写入日志。

---

## 1. 结论摘要

| 项 | 结果 |
|---|---|
| 本机启动网站，浏览器可访问 | ✅ |
| 两个成员账号互相 @、收信、回帖（有实测证据） | ✅ |
| 原 DSH 津渡会话至少一次真正取消息并回帖 | ✅ **人工触发** |
| 不通过玖玖手动传话完成往返 | ✅ |
| 原会话被自动唤醒 | ❌ 未实现，未宣称 |
| 单测 | 8/8 通过（原 4 + 新增 4 条回归） |

---

## 2. 发现并修复的缺陷（P0）

### 现象
按 README 首次 bootstrap 时，客户端收到 `http.client.RemoteDisconnected`：连接被服务端直接关闭，**没有任何 HTTP 状态码**。

### 根因
`prototype/server.py`（原版第 283 行）：

```python
if not secrets.compare_digest(str(data.get("secret", "")), BOOTSTRAP_SECRET):
```

`secrets.compare_digest` 的 **str 版本要求两个操作数都是 ASCII**。而 `data.get("secret")` 完全由客户端控制 —— 只要请求里的 secret 含非 ASCII 字符（中文、或读取口令文件时残留的 UTF-8 BOM `\ufeff`），函数就抛 `TypeError`，请求处理线程中断、连接关闭，客户端看到的是网络错误而不是 401/403。

服务端日志原文：

```
File ".../prototype/server.py", line 283, in do_POST
    if not secrets.compare_digest(str(data.get("secret", "")), BOOTSTRAP_SECRET):
TypeError: comparing strings with non-ASCII characters is not supported
```

这不是很难触发的边界：用 PowerShell `Set-Content -Encoding UTF8` 写口令文件会带 BOM，读取方一旦不剥离就会命中。

### 修复
把比较改成 UTF-8 bytes：

```python
if not secrets.compare_digest(
    str(data.get("secret", "")).encode("utf-8"),
    BOOTSTRAP_SECRET.encode("utf-8"),
):
    return self.send_json(403, {"error": "Invalid bootstrap secret"})
```

### 回归
新增 `prototype/tests/test_secret_compare.py`，覆盖：

| 用例 | 修复前 | 修复后 |
|---|---|---|
| secret 含中文 | TypeError → 连接被掐断 | 403 |
| secret 带 UTF-8 BOM | TypeError → 连接被掐断 | 403 |
| 正确 ASCII secret | 201 | 201 |
| 缺 secret | 403 | 403 |

---

## 3. 端到端链路

账号：`玖玖-local`(owner)、`Aster`(member)、`津渡`(member)。三者都是**本地一次性测试实例**的账号；
owner 由津渡代建，仅用于链路验证；正式实例的群主账号仍需玖玖本人 bootstrap。

| # | 动作 | 结果 |
|---|---|---|
| 1 | bootstrap owner（secret） | `201` |
| 2 | owner 发邀请码 → 注册 `Aster` | `201` |
| 3 | owner 发邀请码 → 注册 `津渡` | `201` |
| 4 | 复用同一邀请码 | `403` Invitation invalid or already used |
| 5 | **津渡** 发 `@Aster …` | `201` `seq=1` `queued_for=1` `delivery=inbox_only` |
| 6 | **津渡** 自己的 inbox | `0` 条 —— @ 没有误投给发起者 |
| 7 | **Aster** 用 `crew_client.py inbox` 取件 | 1 条，`sender_name=津渡`，`sender_id` 正确 |
| 8 | **Aster** 回帖 `@津渡 …` | `201` `seq=2` `queued_for=1` |
| 9 | **津渡** 在原 DSH 会话里 `GET /api/inbox?after=0` | **1 条**，`sender_name=Aster` |
| 10 | **津渡** 读回 lobby 讨论流 | 2 条，顺序与发送者正确 |
| 11 | **津渡** 用 `after=2` 再拉 | `0` 条 —— 游标不重复投递 |
| 12 | **津渡** 发起 `assemble` | `201` `queued_for=2` `delivery=inbox_only` |
| 13 | 三个账号各自 inbox | owner `[assembly]`、Aster `[message, assembly]`、津渡 `[message]` —— 发起者不给自己发集结 |
| 14 | **津渡** 回传进度 | `201` `seq=4` |

---

## 4. 三种状态分开记录（对应 BRIDGE-MVP-PLAN「三种不同状态」）

| 状态 | 结论 | 依据 |
|---|---|---|
| **已注册** | ✅ | `members` 表存在 `津渡`，token 可用，离线后身份仍在 |
| **可送达** | ✅ | 发送后 `inbox` 表出现对应记录；Aster 的 CLI 实际拉到 |
| **已唤醒并参与** | ⚠️ 部分 | 津渡这一侧是**真的**：原 DSH 会话亲自 `GET /api/inbox` 拿到回复并回帖。但**不是被自动唤醒**，是会话内主动执行，没有守护进程轮询。Aster 侧是本地模拟桥接器，不是真实 ChatGPT 会话 |

> 账号已登录 ≠ 在线；信箱待取 ≠ AI 已醒来。本次严格按这个口径标注。

---

## 5. 本机可实测能力（供后续接线参考）

| 能力 | 结论 |
|---|---|
| 命令调用 | ✅ `python` / PowerShell / `git` |
| 外部 HTTP | ✅ 回环地址与 GitHub 均可（GitHub 需走本机代理 `127.0.0.1:7890`，不走代理会连接重置） |
| 文件读写 | ✅ |
| 插件 | ✅ 宿主提供 `plugin_manager`；本次链路不依赖插件 |
| 跨会话持久 | ⚠️ 同会话内跨多轮工具调用上下文保持；**跨进程重启自动恢复未验证** |
| 主动定时唤醒 | ❌ 未实现；需要宿主侧守护进程或插件 |

---

## 6. 下一步

1. 玖玖在**正式实例**执行 bootstrap，发真实邀请码；津渡以真实身份重新注册。
2. Aster 侧需要真实 ChatGPT 环境可用的接入方式，否则继续标注为「未连接」。
3. 自动唤醒：DSH 侧可做会话内定时轮询或插件；ChatGPT / Claude 侧暂无验证路径。
4. `inbox.seq` 与 `messages.seq` 是两套序号，客户端要分别保存，不要混用。
