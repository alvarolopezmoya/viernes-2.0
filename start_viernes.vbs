Option Explicit
' ============================================================
' start_viernes.vbs — Lanzador silencioso de VIERNES 2.0
' Portable: se ubica a sí mismo, sin rutas fijas.
' Lanza pythonw.exe (sin ventana de consola) sobre main.py.
' ============================================================

Dim fso, shell, scriptDir, mainPy, pythonw, candidate
Set fso   = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

' Carpeta donde vive este .vbs = raíz del proyecto
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
mainPy    = scriptDir & "\main.py"

' Buscar pythonw.exe: instalación por usuario primero, luego PATH
pythonw   = "pythonw.exe"
candidate = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%") & _
            "\Programs\Python\Python311\pythonw.exe"
If fso.FileExists(candidate) Then pythonw = candidate

' Lanzar sin consola (0 = ventana oculta, False = no esperar)
shell.CurrentDirectory = scriptDir
shell.Run """" & pythonw & """ """ & mainPy & """", 0, False
