<#
注册"校园网自动认证"计划任务。

设计取舍：不做常驻进程，而是每隔一段时间把脚本拉起一次、跑完就退出。
  * 崩了不用管，下一次触发自动重来
  * 改代码立即生效，不需要重启任何东西
  * 不需要处理"开机时网络还没就绪"

两个触发器：登录时 + 每 30 分钟一次。

用 pythonw.exe 而不是 python.exe：前者不弹黑色控制台窗口。
（代价是看不到控制台输出，但日志本来就会写进 campus-net.log。）

用法（普通权限即可，不需要管理员）：
  powershell -ExecutionPolicy Bypass -File install_task.ps1

⚠️ 本文件必须保存为「UTF-8 带 BOM」。
   Windows PowerShell 5.1 读无 BOM 的 .ps1 时会按系统 ANSI（中文机器上是 GBK）解码，
   里面的中文会被解错，进而把语法搞崩，报 "意外的标记" 之类看不懂的错。
   如果你编辑过这个文件，确认一下 BOM 还在：
     python -c "print(open('install_task.ps1','rb').read().startswith(b'\xef\xbb\xbf'))"
   打印 False 的话补上：
     python -c "p='install_task.ps1'; d=open(p,'rb').read(); open(p,'wb').write(b'\xef\xbb\xbf'+d)"
#>

$ErrorActionPreference = "Stop"

$TaskName   = "CampusNetAutoLogin"
$EveryMins  = 30
$TimeLimitM = 10      # 单次执行上限。登录最慢可能要一分钟，留足余量
$Root       = Split-Path -Parent $MyInvocation.MyCommand.Path
$Script     = Join-Path $Root "login.py"

if (-not (Test-Path $Script)) { throw "找不到 login.py: $Script" }

# 找 pythonw.exe（和 python.exe 同目录）
$PyExe = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
if (-not $PyExe) { throw "PATH 里找不到 python.exe，请先确认 Python 装好了" }
$PyW = Join-Path (Split-Path -Parent $PyExe) "pythonw.exe"
if (Test-Path $PyW) { $Python = $PyW } else { $Python = $PyExe }

$User = "$env:USERDOMAIN\$env:USERNAME"

Write-Host "解释器 : $Python"
Write-Host "脚本   : $Script"
Write-Host "工作目录: $Root"
Write-Host "运行身份: $User"
Write-Host ""

# 已存在就先删掉，避免重复注册
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已移除旧的同名任务"
}

$Action = New-ScheduledTaskAction `
    -Execute $Python `
    -Argument "`"$Script`" --check" `
    -WorkingDirectory $Root

# 触发器 1：登录时
$T1 = New-ScheduledTaskTrigger -AtLogOn -User $User

# 触发器 2：从现在起 2 分钟后开始，每 30 分钟一次。
# 故意不写 -RepetitionDuration：不指定持续时间的重复就是无限重复。
# （写 [TimeSpan]::MaxValue 会被计划任务拒绝，说持续时间超出范围。）
$T2 = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(2) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMins)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeLimitM) `
    -MultipleInstances IgnoreNew

# LogonType Interactive：必须用交互式身份，否则 Chrome 无法显示窗口
$Principal = New-ScheduledTaskPrincipal `
    -UserId $User `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger @($T1, $T2) `
    -Settings $Settings `
    -Principal $Principal `
    -Description "校园网自动认证：定时探测，未认证时自动用浏览器登录。日志见 campus-net.log" | Out-Null

Write-Host "已注册计划任务: $TaskName"
Write-Host ""
Write-Host "触发器: 登录时 + 每 $EveryMins 分钟"
Write-Host "查看:   schtasks /Query /TN $TaskName /V /FO LIST"
Write-Host "手动跑: schtasks /Run /TN $TaskName"
Write-Host "删除:   powershell -ExecutionPolicy Bypass -File uninstall_task.ps1"
Write-Host ""
Write-Host "注意: 任务以交互式身份运行，所以只在【已登录】状态下才会触发。"
Write-Host "      也就是开机后需要先进入桌面，浏览器才弹得出来。"
