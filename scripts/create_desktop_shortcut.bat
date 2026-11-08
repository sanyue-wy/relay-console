@echo off
chcp 65001 >nul
rem 创建桌面快捷方式：只生成 .lnk 文件，不写注册表、不装服务、不设自启。
powershell -NoProfile -Command "$d='%~dp0'; $w=New-Object -ComObject WScript.Shell; $p=[Environment]::GetFolderPath('Desktop')+'\Relay Console.lnk'; $s=$w.CreateShortcut($p); $s.TargetPath=Join-Path $d 'RelayConsole.exe'; $s.WorkingDirectory=$d; $s.IconLocation=Join-Path $d 'RelayConsole.exe'; $s.Description='Relay Console 控制台'; $s.Save(); Write-Host ('已创建: '+$p); Write-Host '仅生成 .lnk 文件，未写注册表。删除该文件即彻底移除。'"
pause
