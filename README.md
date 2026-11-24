# campus-net-autologin

Windows 上的校园网自动认证脚本：探测外网通不通，需要认证时用 Selenium 驱动本机
Chrome / Edge 打开门户，填学号密码、识别验证码、选运营商、点登录，全程无人值守。

配套 Windows 计划任务：登录系统时、每 5 分钟、网络状态变化时、从睡眠唤醒时各检查一次。

## 它解决什么问题

| 场景 | 处理方式 |
|---|---|
| 开机 / 唤醒后要手动点一次认证 | 探测到未认证就自动登录 |
| 掉线后不会自己恢复 | 每 5 分钟探测一次，掉了就重登 |
| 门户有验证码、要选运营商 | 走真实浏览器完成整条流程，不模仿 HTTP 接口 |
| Chrome 自动升级导致驱动失效 | 自动把 chromedriver 对齐到本机 Chrome 版本 |
| 开机瞬间网卡还没就绪 | 门户不可达时重探几次，不用白等那几十秒 |

## 适用学校

默认配置直接适配**四川工业科技学院**的门户（`http://10.71.29.181`），`selectors`
也是照这个门户量出来的 —— 同校的话，克隆下来只需要填自己的学号和密码：

```bash
python login.py --setup
```

其他学校需要改 `config.json` 里的 `portal`，并按 `python login.py --dump-form`
的输出重新校准 `selectors`。

## 快速开始

1. 安装 Python 3.10+ 和 Chrome（或 Edge）
2. `pip install -r requirements.txt`
3. 复制 `config.example.json` 为 `config.json`，改 `username`、`portal`，
   以及（换学校时）`selectors`
4. `python login.py --setup` 保存密码（DPAPI 加密后存在 `secret.dat`）
5. 双击 `test-login.bat` 先手动验证一次
6. `powershell -ExecutionPolicy Bypass -File install_task.ps1` 注册计划任务

完整步骤和排查方法见 [使用说明.txt](使用说明.txt)。

## 命令

| 命令 | 作用 |
|---|---|
| `python login.py --check` | 探测并按需登录（计划任务走这条） |
| `python login.py --login` | 跳过探测，强制登录一次 |
| `python login.py --setup` | 交互式保存密码 |
| `python login.py --dump-form` | 打开登录页并打印表单元素，用于校准 selectors |
| `python login.py --update-driver` | 把 chromedriver 更新到与本机 Chrome 匹配的版本 |

退出码：`0` 已在线或登录成功；`1` 未能上网；`2` 配置或环境错误。

## 几个关键设计

- **以"外网能不能出去"为唯一成功标准**（`src/probe.py`），而不是看页面文案。
- **驱动与浏览器版本**：优先用与 Chrome 主版本一致的 chromedriver；没有就用手上
  最新的去试（实测差一个主版本通常能用），起不来再交给 Selenium Manager。
  已经在线时自动补齐匹配版本（`src/driver_update.py`，优先国内镜像）。
- **刚唤醒时网络会抖**：识别 Chrome 的网络错误页（`ERR_NETWORK_CHANGED` 等）并
  自动重新加载；仍未恢复时按 `network_unstable` 收尾，不计入失败、不触发退避。
- **防锁号**：失败后退避 15 分钟，6 小时内失败 6 次就停下来等人工处理。

## 目录

```
login.py              CLI 入口（--check / --login / --setup / --dump-form / --update-driver）
src/probe.py          外网探测
src/browser_login.py  浏览器自动化：导航、采集、填表、提交、判定
src/driver_update.py  chromedriver 版本对齐与下载
src/captcha.py        验证码识别（ddddocr，本地推理）
src/config.py         配置加载与校验
src/state.py          跨运行的失败计数 / 退避 / 锁号保护
src/secret.py         密码加密存储（Windows DPAPI）
install_task.ps1      注册 / 卸载计划任务
```

## 开发

```bash
python -m pytest -q
```

测试不联网、不起浏览器：HTTP 和 WebDriver 都换成替身。

## 说明

- 只在 Windows 上验证过：计划任务、DPAPI、驱动版本检测都依赖 Windows。
- 驱动自动更新目前只覆盖 Chrome；Edge 的驱动由 Selenium Manager 负责。
- 本工具仅供在自己的账号、自己的设备上使用。
