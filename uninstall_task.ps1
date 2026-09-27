<#
删除"校园网自动认证"计划任务。

用法：
  powershell -ExecutionPolicy Bypass -File uninstall_task.ps1
#>

$ErrorActionPreference = "Stop"
$TaskName = "CampusNetAutoLogin"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除计划任务: $TaskName"
} else {
    Write-Host "没有找到计划任务 $TaskName（可能本来就没装）"
}
