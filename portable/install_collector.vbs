Option Explicit

Dim fso, shell, sourceDir, targetDir, secret, relayUrl, config, runner, runCommand, sourceFile, targetFile, collectorSource
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

sourceDir = fso.GetParentFolderName(WScript.ScriptFullName)
targetDir = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & "\ArendaKGCollector"
relayUrl = "https://statutory-mallissa-2danmiller-f1c1b08d.koyeb.app/internal/lalafo/ingest"

secret = InputBox("Введите relay-ключ Arenda.KG:", "Установка сборщика Lalafo")
If Len(Trim(secret)) = 0 Then
  MsgBox "Установка отменена: relay-ключ не указан.", 48, "Arenda.KG"
  WScript.Quit 1
End If

If Not fso.FolderExists(targetDir) Then fso.CreateFolder targetDir

' Re-save the ASCII-only collector instead of copying it. This strips
' Mark-of-the-Web and avoids a BOM, which legacy Windows Script Host treats as
' executable characters rather than an encoding marker.
Set sourceFile = fso.OpenTextFile(fso.BuildPath(sourceDir, "lalafo_collector.js"), 1, False, 0)
collectorSource = sourceFile.ReadAll
sourceFile.Close
Set targetFile = fso.CreateTextFile(fso.BuildPath(targetDir, "lalafo_collector.js"), True, False)
targetFile.Write collectorSource
targetFile.Close

config = "{""relayUrl"":""" & relayUrl & """,""relaySecret"":""" & Replace(secret, """", "") & """,""intervalMinutes"":120}"
Dim stream
Set stream = fso.OpenTextFile(fso.BuildPath(targetDir, "collector-config.json"), 2, True, -1)
stream.Write config
stream.Close

runner = shell.ExpandEnvironmentStrings("%SystemRoot%\System32\wscript.exe")
runCommand = """" & runner & """ """ & fso.BuildPath(targetDir, "lalafo_collector.js") & """"

' Use the per-user Run registry key instead of a Startup shortcut.  This stays
' reliable when the Windows profile name contains non-Latin characters.
shell.RegWrite "HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ArendaKGLalafoCollector", runCommand, "REG_SZ"
shell.Run runCommand, 0, False

MsgBox "Сборщик установлен и запущен в фоне. Автозапуск включен.", 64, "Arenda.KG"
