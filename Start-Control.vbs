Option Explicit
Dim shell, files, root, args, electron, python, command
Set shell = CreateObject("WScript.Shell")
Set files = CreateObject("Scripting.FileSystemObject")
root = files.GetParentFolderName(WScript.ScriptFullName)
electron = files.BuildPath(root, "runtime\electron\electron.exe")
python = files.BuildPath(root, "runtime\python\python.exe")
If Not files.FileExists(electron) Or Not files.FileExists(python) Then
  MsgBox "The portable runtime is missing. Use Start-Control.ps1 for a source checkout.", 48, "Aieyra Control"
  WScript.Quit 1
End If
shell.CurrentDirectory = root
command = Chr(34) & electron & Chr(34) & " " & Chr(34) & files.BuildPath(root, "desktop") & Chr(34) & " " & Chr(34) & "--python=" & python & Chr(34)
If WScript.Arguments.Count > 0 Then
  If WScript.Arguments(0) = "--quit" Then command = command & " --quit"
End If
shell.Run command, 0, False
