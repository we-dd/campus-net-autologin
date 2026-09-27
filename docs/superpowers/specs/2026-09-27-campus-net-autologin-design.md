# 校园网自动认证脚本 — 设计文档

- 日期：2026-09-27
- 状态：待用户评审
- 目标平台：本机 Windows 11（用户账户 `De Ronaldinho`）

---

## 1. 目标与非目标

### 目标

在无人值守的情况下，当本机处于未认证状态时，自动完成校园网网页认证（锐捷 ePortal + SAM 统一身份认证），使网络恢复可用。

成功标准：电脑开机后、或中途掉线后，不需要人工干预就能重新上网；人工干预只在验证码 OCR 连续失败时才需要（弹窗手填一次）。

### 非目标（明确不做）

- 不做图形设置界面，配置手改 `config.json`
- 不做托盘图标、不做开机动画
- 不做多账号、多网卡切换
- 不做跨设备分发。这是**只给本人账号、本机使用**的工具。验证码是校方有意设置的反自动化措施，不把绕过逻辑做成通用/分发的工具

---

## 2. 环境事实（已在本机实测确认）

| 项 | 值 |
|---|---|
| 操作系统 | Windows 11 Home China 10.0.26100 |
| Python | 3.14.3 |
| pip | 25.3 |
| 已安装 | `requests`、`Pillow` 12.2.0、`numpy` 2.4.4、`onnxruntime` 1.26.0、`opencv-python` 4.13.0.92 |
| 待安装 | `ddddocr` 1.6.1（**唯一新增依赖**，重依赖已齐备） |
| Shell | Git Bash（无 make、无 Linux、无容器） |

抓包得到的认证相关端点：

- 认证服务器：`10.71.29.181`
- 登录页（GET，打开表单）：
  `http://10.71.29.181/sam-sso/login?flowSessionId=<动态>&customPageId=caa2cb3fa72f413ebaade57bf4bd4fe6&preview=false&appType=normal&language=zh-CN&userIp=<本机IP>&userMac=<MAC>&accept-language=zh-CN`
- ePortal 服务选择页：`http://10.71.29.181/eportal/network/serviceSelection`

由上面 URL 得出的三条设计约束：

1. `flowSessionId` 是**每次登录流程现生成的一次性会话号**，不能写死，必须每轮先请求页面拿到新的
2. `userIp`、`userMac` 是**运行时动态值**（DHCP 会变、换网卡会变），必须运行时读取，不能写死
3. 验证码与 HTTP 会话绑定。拉验证码和提交登录**必须使用同一个 `requests.Session()`**，否则服务端校验必然不通过

验证码：**存在图形验证码**（已由用户确认）。字符数、是否含干扰线待样本实测。

---

## 3. 目录结构

```
C:\Users\De Ronaldinho\campus-net\
├── config.json              配置：账号、服务器地址、重试次数、探测地址列表
├── secret.dat               密码（Windows DPAPI 加密，仅本账户可解）
├── state.json               跨运行的失败计数与时间戳（防锁号用）
├── campus-net.log           运行日志（滚动）
├── samples\                 验证码样本，用于离线测准确率
├── src\
│   ├── config.py            读配置、校验、给默认值
│   ├── secret.py            DPAPI 加解密封装
│   ├── probe.py             判断"当前是否已联网"
│   ├── captcha.py           取验证码图片 + ddddocr 识别
│   ├── portal.py            登录流程编排，对外只暴露 login(cfg) -> Result
│   ├── notify.py            弹窗输入 + 日志
│   └── logsetup.py          日志初始化
├── login.py                 CLI 入口
├── install_task.ps1         注册/卸载计划任务
└── docs\superpowers\specs\  本设计文档
```

各模块独立可测：`probe`、`secret`、`config` 不依赖网络；`captcha` 只依赖网络 + 本地模型；`portal` 依赖前三者。

---

## 4. 模块职责与接口

### 4.1 `config.py`

```python
def load(path: str = "config.json") -> Config
```

- 读 JSON，缺失字段填默认值，类型不对时报错并给出可读信息
- 配置项见第 9 节

### 4.2 `secret.py`

```python
def save_password(password: str, path: str) -> None
def load_password(path: str) -> str
```

路径是必填参数，不给默认值：真实调用方（`--setup`、`portal._build_payload`）传的都是 `cfg.secret_path`，
而 `cfg.secret_path` 是按配置文件所在目录推导的，**不是当前工作目录**。给一个相对路径的默认值
反而会诱导出"计划任务以 System32 为工作目录时把密码写到系统目录"这类问题。

- 用 `ctypes` 直接调 Windows DPAPI 的 `CryptProtectData` / `CryptUnprotectData`
- 不引入 `keyring`、`pywin32` 等额外依赖
- 加密域绑定当前 Windows 账户，换账户/换机器解不开（这是预期行为，不是 bug）

### 4.3 `probe.py`

```python
class State(Enum):
    ONLINE = "online"          # 已联网，什么都不用做
    UNAUTHED = "unauthed"      # 被 portal 劫持，需要登录
    PORTAL_DOWN = "portal_down"  # portal 服务器都摸不到，网络层面的故障
    UNKNOWN = "unknown"        # 外网不通但也没看到 portal 劫持

def check(cfg: Config, session: requests.Session) -> State
```

判断逻辑：依次请求配置里的候选探测地址，`allow_redirects=False`：

- 返回 200 且内容匹配该地址的预期特征 → `ONLINE`
- 返回 3xx 跳转，或返回 HTML（被 portal 劫持） → `UNAUTHED`
- 连接超时 / DNS 失败 → 换下一个地址

所有候选地址都不通之后，**再做一次区分**：直接请求 `cfg.portal` 根地址：

- portal 也超时/不可达 → `PORTAL_DOWN`
- portal 可达，但外网不通 → `UNAUTHED`

`PORTAL_DOWN` 与 `UNKNOWN` 都**不触发登录尝试**，只记日志。理由：这两种情况下登录注定失败，而每一次失败都在累积锁号风险，白白消耗重试额度。

> 之所以用"外部探测地址 + 判定重定向"，而不是直接问 portal 服务器，是因为认证成功与否的唯一可靠标志就是"外网能不能正常出去"。

默认探测地址列表（可在 config 覆盖）：

- `http://www.msftconnecttest.com/connecttest.txt`，预期内容前缀 `Microsoft Connect Test`
- `http://connect.rom.miui.com/generate_204`，预期 HTTP 204

### 4.3b `logsetup.py`

```python
def init(log_path: str, level: str) -> None
```

统一日志格式（时间 + 级别 + 模块），写到 `campus-net.log`，单文件超过 1 MB 轮转到 `.1`。所有模块通过标准 `logging` 取 logger，不各自开文件。

### 4.4 `captcha.py`

```python
def fetch_image(session: requests.Session, cfg: Config) -> bytes
def recognize(image_bytes: bytes) -> str
```

- `fetch_image`：带着与登录同一个 `Session` 去请求验证码图片接口，返回原始字节。**不落盘** —— 样本由人工在登录页上另存（见第 10 节），脚本不该在每次后台运行时往 `samples/` 里堆图片
- `recognize`：用 ddddocr 本地识别，返回识别出的字符串（去空白）
- ddddocr 只初始化一次（模型加载有开销），模块级单例

### 4.5 `portal.py`

```python
@dataclass
class Result:
    ok: bool
    reason: str                    # "success" | "bad_captcha" | "bad_credentials" | "network" | "secret" | "unknown"
    detail: str = ""
    captcha_image: bytes | None = None   # 带回去给弹窗兜底用

def login(cfg: Config, session: requests.Session, captcha_text: str | None = None) -> Result
```

`reason` 的取值决定 `do_login` 怎么处理，尤其是**要不要计入锁号窗口**：

| reason | 是否重试 | 是否计入锁号窗口 | 退出码 |
|---|---|---|---|
| `success` | — | 清零 | 0 |
| `bad_captcha` | 是（换一张） | 是 | 1 |
| `bad_credentials` | **否** | **是**（确实打到了服务器） | 1 |
| `network` | 是（1 次） | 是 | 1 |
| `secret` | **否** | **否**（本地密码文件问题，根本没发出请求） | 2 |
| `unknown` | 是 | 是 | 1 |

`secret` 必须单独成一个 reason：把它归进 `unknown` 会导致密码文件坏掉时不停重试、耗掉锁号窗口，
最后弹出「连续失败次数过多，已暂停自动登录，避免账号被锁」—— 一个**假警报**，
而真正的原因（重新运行 `--setup`）反被埋掉。

职责边界（**这是本设计的核心接口**）：

- `login()` 内部完成：拉登录页 → 提取 `flowSessionId`/`queryString` → 取验证码 → 识别（`captcha_text` 为 None 时走 OCR，非 None 时用传入值）→ 提交 → 解析响应
- 调用方（`login.py`）只关心返回的 `Result`，不关心中间步骤
- 失败重试的编排在 `login.py`，不在 `portal.py`。`portal.py` 每次调用只做**一次**完整尝试

**提交步骤的适配边界**：提交端点和字段取决于抓包结果，尚未确定。因此 `portal.py` 内部把提交抽象成一个函数：

```python
def _submit(session, cfg, payload: dict) -> tuple[bool, str]
```

实现时按抓包结果填入。两种已知可能：

- **锐捷标准接口**：`POST /eportal/InterFace.do?method=login`，表单字段形如 `userId` / `password` / `service` / `queryString` / `validcode`。响应是 JSONP 包装的 JSON，含 `result` 字段。这条路最省事
- **走 `/sam-sso/` 提交、或密码在 JS 里加密**：需要把前端加密逻辑（常见是 `strEnc` 一类的 DES 变体）用 Python 复刻。工作量明显更大，但可行

无论哪种，`login()` 的对外接口不变，`login.py`、调度、弹窗都不受影响。这是把"未知"隔离在一个函数里的目的。

### 4.6 `notify.py`

```python
def ask_captcha(image_bytes: bytes, cfg: Config) -> str | None
def notify(title: str, message: str) -> None
```

- `ask_captcha`：用 `tkinter`（Python 自带，零依赖）弹小窗，显示验证码图片，账号密码已填好，用户输入 4 个字符回车即返回；用户关窗则返回 `None`
- 窗口置顶，避免被别的窗口盖住
- 由任务计划以"仅在用户登录时运行"启动，保证有桌面会话可弹窗；若确实弹不出来（会话不可交互），退化为写日志 + 放弃本次，不崩溃

### 4.7 `login.py`（CLI 入口）

| 参数 | 行为 |
|---|---|
| `--check` | 供计划任务调用。按 4.3 的四种状态分流；`ONLINE` 时静默退出，`PORTAL_DOWN`/`UNKNOWN` 只记日志，`UNAUTHED` 才执行登录 |
| `--login` | 强制登录一次，跳过探测。手动调试用 |
| `--setup` | 交互式保存账号/密码（密码走 DPAPI） |
| `--test-captcha` | **读本地 `samples/` 目录**里人工存的验证码，统计识别准确率并给出路线判定。用于第 10 节的先导测试。**不联网抓图**——先导测试的全部意义就是别去打扰认证服务器 |

退出码约定：`0` 已在线或登录成功；`1` 登录失败；`2` 配置/环境错误。便于任务计划与手工排查。

---

## 5. 一次登录的完整流程

```
1. 探测 → State
   ├─ ONLINE      → 清失败计数，静默结束（退出码 0）
   ├─ PORTAL_DOWN → 记日志，结束（退出码 0，不算失败）
   ├─ UNKNOWN     → 记日志，结束（退出码 0，不算失败）
   └─ UNAUTHED    → 继续
2. 检查防锁号状态：是否已进入"停止自动登录"状态？
   ├─ 是 → 弹窗提示，结束（退出码 1）
   └─ 否 → 继续
3. GET portal 页面 → 拿到新的 flowSessionId / queryString
4. GET 验证码图片（同一 Session） → bytes
5. ddddocr 识别 → 4 字符
6. POST 提交（账号 + 密码 + 验证码 + queryString）
7. 解析响应
   ├─ 成功 → 清空失败计数，写日志，结束
   └─ 失败 → 区分原因：
       ├─ 验证码错误 → 回到第 4 步换一张，最多重试 5 次
       ├─ 账号密码错误 → 不重试，立即弹窗告警（重试只会加速锁号）
       └─ 网络/未知 → 重试 1 次，仍失败则记录并放弃
8. 5 次 OCR 全部失败 → 转入弹窗模式：显示最后一张验证码图片，用户手填一次
9. 用户填的也失败 → 放弃本轮，写日志，退出码 1
```

---

## 6. 触发与调度

用 Windows 计划任务，**不做常驻进程**。理由：常驻进程要处理守护、崩溃重启、开机时网络未就绪、改代码后需重启等问题，麻烦且脆弱。改为定时拉起脚本，脚本自己判断该不该动作：

- 崩溃了不用管，下一个触发点自动重来
- 每次只运行 1~2 秒，开销可忽略
- 改代码立即生效，不需要重启任何东西

### 触发器

| 触发器 | 时机 |
|---|---|
| 登录时 | 开机登录后立刻跑一次（覆盖"刚开机没网"） |
| 重复 | 每 30 分钟一次 |
| 工作站解锁 | 睡眠唤醒后立刻跑一次 |
| 网络配置变更 | `Microsoft-Windows-NetworkProfile/Operational` 事件（WiFi/网线切换） |

定时轮询按用户要求设为 **30 分钟**。加后面这组事件触发器的目的：不提高轮询频率的前提下，覆盖"最容易掉线"的几个瞬间，使实际恢复延迟基本接近秒级。

### 任务设置

- 以当前用户身份运行，勾选"仅在用户登录时运行"（弹窗需要桌面会话）
- "如果任务运行时间超过 3 分钟，停止任务"——防脚本卡死堆积
- 不勾"使用最高权限运行"，普通权限足够（不需要改系统网络配置）

---

## 7. 失败处理与防锁号

学校的认证系统连续失败会锁账号，这是本设计里最需要克制的部分：

- **单次运行内**最多重试 5 次（每次换新验证码）
- **单次运行内**累计失败 3 次 → 立即停止并弹窗告警，不再继续试
- **跨运行**失败计数记在 `state.json`，6 小时窗口内累计失败 6 次 → 停止自动登录，改为每次只弹窗提示，直到人工成功登录一次才清零。窗口外的旧记录自动过期，不影响计数
- 检测到"账号密码错误"这类**确定性失败**时，不做任何重试，直接弹窗
- 成功一次即清零所有计数

> 设计意图：宁可少试，也不要因为脚本死循环把账号试锁了。锁号带来的麻烦远大于掉线。

---

## 8. 凭据与安全

- 账号明文存 `config.json`（无敏感性）
- **密码用 DPAPI 加密存 `secret.dat`**，绑定当前 Windows 账户。脚本不保存明文密码，配置文件里不出现密码
- 日志里不打印密码和验证码原文；账号做脱敏（只留前 3 位）
- 验证码样本落盘 `samples/` 便于日后评估，其中不含任何凭据信息
- 本工具仅本人本机使用，不做分发

---

## 9. 配置文件示例

```json
{
  "username": "学号",
  "portal": "http://10.71.29.181",
  "captcha_max_retry": 5,
  "run_max_fail": 3,
  "window_hours": 6,
  "window_max_fail": 6,
  "probe_urls": [
    { "url": "http://www.msftconnecttest.com/connecttest.txt", "expect_text": "Microsoft Connect Test" },
    { "url": "http://connect.rom.miui.com/generate_204", "expect_status": 204 }
  ],
  "probe_timeout": 5,
  "log_level": "INFO"
}
```

---

## 10. 测试策略

### 先导测试（第一步就做，决定后续路线）

抓 30 张真实验证码图片存入 `samples\`，**文件名写成图片里的字符**（如 `3f7k.png`，重名就加序号 `3f7k_1.png`）。存图的人本来就在看那几个字符，顺手命名即可，不需要额外逐张标注。

然后跑 `login.py --test-captcha`，自动统计 ddddocr 命中率。判定标准：

| 准确率 | 后续路线 |
|---|---|
| ≥ 80% | 按本设计走：OCR 主力 + 弹窗兜底 |
| 60% ~ 80% | 仍走 OCR 主力，但重试次数提到 8，并接受偶发弹窗 |
| < 60% | **退回方案 B**：以弹窗为主，OCR 只做尝试性预填。此时不应继续按本设计的复杂度实现 |

这个测试必须最先做。它花的成本极小，但能避免"写完一整套才发现识别根本不准"。

### 单元测试（不依赖网络）

- `probe`：mock 各种响应（200 正常 / 302 劫持 / 超时 / 返回 HTML），断言判定正确
- `secret`：DPAPI 加解密往返一致；损坏文件报错可读
- `config`：缺字段填默认、类型错误报错

### 集成测试（真实网络，注意次数）

- `--login` 手动跑通一次
- 断开认证后跑 `--check`，确认自动恢复
- **弹窗路径**：临时把 ddddocr 打桩成必定失败，验证弹窗确实能弹出并提交成功
- 计划任务：手动触发一次，确认用户会话下能正常运行、弹窗可见

所有集成测试注意控制次数，避免触发学校风控。

---

## 11. 未决事项

**唯一一项**：登录提交的确切端点与字段（`_submit` 的实现）。

- 需要一份点"登录"时发出的请求抓包（URL + 方法 + 表单字段，密码脱敏）
- 若为锐捷标准 `InterFace.do?method=login`，直接按表单提交实现
- 若走 `/sam-sso/` 或密码前端加密，需额外复刻加密逻辑
- 该未知已被隔离在 `portal._submit()` 一个函数内，不影响其余所有模块的实现与测试

---

## 12. 实施顺序

1. **验证码准确率实测**（决定路线，最先做）
2. `config.py` + `secret.py` + `probe.py` + 对应单元测试（不依赖抓包，可马上做）
3. `portal.py`（依赖抓包结果，填入 `_submit`）
4. `captcha.py` + `notify.py`（弹窗兜底）
5. `login.py` CLI 与重试/防锁号逻辑
6. `install_task.ps1` 与计划任务实测
7. 整体跑通，观察几天，按实际表现调参
