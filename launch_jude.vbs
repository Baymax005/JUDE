Option Explicit

Dim fso, shell, env, rootDir, pythonwPath
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = rootDir

Set env = shell.Environment("PROCESS")
env("JUDE_ROOT") = rootDir
env("PIP_CACHE_DIR") = rootDir & "\cache\pip"
env("HF_HOME") = rootDir & "\cache\huggingface"
env("TORCH_HOME") = rootDir & "\cache\torch"
env("XDG_CACHE_HOME") = rootDir & "\cache\xdg"
env("TEMP") = rootDir & "\cache\tmp"
env("TMP") = rootDir & "\cache\tmp"
env("OLLAMA_MODELS") = rootDir & "\models\ollama"
env("PYTHONDONTWRITEBYTECODE") = "1"
env("PYTHONPATH") = rootDir

pythonwPath = rootDir & "\venv\Scripts\pythonw.exe"
shell.Run """" & pythonwPath & """ -m app.main", 0, False
