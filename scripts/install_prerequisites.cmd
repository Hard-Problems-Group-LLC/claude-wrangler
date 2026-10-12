@echo off
rem install_prerequisites.cmd - Windows wrapper for install_prerequisites.py.
rem
rem Finds a native Python 3.9+ the same way install.cmd does and runs
rem scripts\install_prerequisites.py with every argument forwarded. When no
rem Python exists it offers, with consent, to install one for the current
rem user with winget; it then continues with that interpreter, or asks the
rem operator to open a new terminal and rerun when it cannot find it yet.
rem
rem Consent without Python: --dry-run prints the winget command, --yes runs
rem it without prompting, and otherwise one prompt asks; a run whose input
rem is not a console reads no answer and refuses. Only the first nine
rem arguments are inspected for these flags here; install_prerequisites.py
rem parses everything once Python exists. Nothing is ever elevated:
rem "winget install --scope user" needs no Administrator rights.
rem
rem See install.cmd for the parsing constraints this file follows: ASCII
rem only, no labels or GOTO, interpreters judged by exit code and never
rem captured from program output, and the argument list used only on the
rem final top-level command line.
setlocal EnableExtensions DisableDelayedExpansion
set "NoDefaultCurrentDirectoryInExePath=1"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TK_HELPER=%~dp0install_prerequisites.py"
set "TK_PROBE=import sys; raise SystemExit(not (sys.hexversion // 0x03090000 and sys.platform == 'win32'))"
set "TK_PY="
set "TK_PYARG="
set "TK_OVERRIDE="
set "TK_HELP="
set "TK_YES="
set "TK_DRY="
set "TK_ANSWER="
set "TK_WINGET="
set "TK_INSTALLED="
if "%~1"=="-h" set "TK_HELP=1"
if "%~1"=="--help" set "TK_HELP=1"
if "%~1"=="/?" set "TK_HELP=1"
if "%~1"=="--yes" set "TK_YES=1"
if "%~2"=="--yes" set "TK_YES=1"
if "%~3"=="--yes" set "TK_YES=1"
if "%~4"=="--yes" set "TK_YES=1"
if "%~5"=="--yes" set "TK_YES=1"
if "%~6"=="--yes" set "TK_YES=1"
if "%~7"=="--yes" set "TK_YES=1"
if "%~8"=="--yes" set "TK_YES=1"
if "%~9"=="--yes" set "TK_YES=1"
if "%~1"=="--dry-run" set "TK_DRY=1"
if "%~2"=="--dry-run" set "TK_DRY=1"
if "%~3"=="--dry-run" set "TK_DRY=1"
if "%~4"=="--dry-run" set "TK_DRY=1"
if "%~5"=="--dry-run" set "TK_DRY=1"
if "%~6"=="--dry-run" set "TK_DRY=1"
if "%~7"=="--dry-run" set "TK_DRY=1"
if "%~8"=="--dry-run" set "TK_DRY=1"
if "%~9"=="--dry-run" set "TK_DRY=1"
if not exist "%TK_HELPER%" (
  >&2 echo install_prerequisites.cmd: missing "%TK_HELPER%"; this checkout is incomplete.
  exit /b 1
)
if defined THEKNOWLEDGE_BOOTSTRAP_PYTHON set "TK_OVERRIDE=%THEKNOWLEDGE_BOOTSTRAP_PYTHON:"=%"
if defined TK_OVERRIDE (
  call "%TK_OVERRIDE%" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%TK_OVERRIDE%"
)
if defined TK_OVERRIDE if not defined TK_PY (
  >&2 echo install_prerequisites.cmd: THEKNOWLEDGE_BOOTSTRAP_PYTHON does not name a native Python 3.9+; not falling back.
  exit /b 1
)
if not defined TK_PY (
  call py -3 -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=py" && set "TK_PYARG=-3"
)
if not defined TK_PY if defined PATH for %%D in ("%PATH:;=" "%") do if not defined TK_PY if not "%%~D"=="" if /i "%%~fD"=="%%~D" if exist "%%~D\python.exe" (
  call "%%~D\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~D\python.exe"
)
if not defined TK_PY if defined LOCALAPPDATA for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  call "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY if defined LOCALAPPDATA for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  call "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY if defined ProgramFiles for /d %%D in ("%ProgramFiles%\Python3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  call "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY if defined USERPROFILE for /d %%D in ("%USERPROFILE%\.pyenv\pyenv-win\versions\3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  call "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY if defined TK_HELP (
  echo Usage: scripts\install_prerequisites.cmd [options]
  echo.
  echo Install the prerequisites this project needs when they are missing:
  echo Python 3.9+, Git, and Git for Windows' bash.
  echo.
  echo Options available before Python exists:
  echo   --yes       Install Python for the current user with winget, no prompt.
  echo   --dry-run   Print the winget command without running it.
  echo   -h, --help  Show this help.
  echo.
  echo The full option list, including --dev, is shown once Python 3.9+ exists.
  exit /b 0
)
if not defined TK_PY (
  >&2 echo install_prerequisites.cmd: no native Python 3.9+ found. Tried THEKNOWLEDGE_BOOTSTRAP_PYTHON,
  >&2 echo install_prerequisites.cmd: py -3, each python.exe on PATH, and the default install locations.
  where /q $PATH:winget && set "TK_WINGET=1"
)
if not defined TK_PY if not defined TK_WINGET (
  >&2 echo install_prerequisites.cmd: winget is not available. Install Python 3.9+ from
  >&2 echo install_prerequisites.cmd: https://www.python.org/downloads/windows/ or get winget with
  >&2 echo install_prerequisites.cmd: App Installer from the Microsoft Store, then open a new terminal
  >&2 echo install_prerequisites.cmd: and rerun this script.
  exit /b 1
)
set "TK_WINGET_CMD=winget install --id Python.Python.3.12 --exact --source winget --scope user --accept-package-agreements --accept-source-agreements"
if defined TK_YES set "TK_WINGET_CMD=%TK_WINGET_CMD% --silent"
if not defined TK_PY if defined TK_DRY (
  echo -^> %TK_WINGET_CMD%
  echo install_prerequisites.cmd: dry run; Python was not installed.
  exit /b 0
)
if not defined TK_PY if not defined TK_YES (
  echo install_prerequisites.cmd: Python 3.9+ is required. This command installs it for
  echo the current user and accepts the package and source agreements:
  echo   %TK_WINGET_CMD%
  set /p "TK_ANSWER=Install Python 3.12 for the current user now? [y/N] "
)
if not defined TK_PY if not defined TK_YES if /i not "%TK_ANSWER%"=="y" if /i not "%TK_ANSWER%"=="yes" (
  echo.
  >&2 echo install_prerequisites.cmd: Python was not installed. Rerun with --yes to install
  >&2 echo install_prerequisites.cmd: it without a prompt, or install Python 3.9+ yourself.
  exit /b 1
)
if not defined TK_PY (
  call %TK_WINGET_CMD% || set "TK_INSTALLED=failed"
)
if "%TK_INSTALLED%"=="failed" (
  >&2 echo install_prerequisites.cmd: winget could not install Python.
  exit /b 1
)
if not defined TK_PY set "TK_INSTALLED=1"
if not defined TK_PY if defined LOCALAPPDATA for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  call "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY (
  echo install_prerequisites.cmd: Python was installed. Open a new terminal so PATH
  echo includes it, then rerun scripts\install_prerequisites.cmd.
  exit /b 1
)
if defined TK_INSTALLED (
  echo install_prerequisites.cmd: Python was installed; continuing with it. New terminals
  echo will also find it on PATH.
)
"%TK_PY%" %TK_PYARG% "%TK_HELPER%" %*
set "CMD_EXIT=%ERRORLEVEL%"
exit /b %CMD_EXIT%
