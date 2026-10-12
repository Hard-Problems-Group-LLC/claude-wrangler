@echo off
rem install.cmd - native Windows peer of install.sh.
rem
rem Finds a native Python 3.9+ and runs scripts\install-stage-1-windows.py,
rem the Windows policy gate, which runs the unchanged
rem scripts\install-stage-2.py with every argument forwarded and returns
rem its exit code. Run "install.cmd --help" for usage.
rem
rem Interpreter discovery; the first candidate that passes the probe wins:
rem   1. THEKNOWLEDGE_BOOTSTRAP_PYTHON when set, with no fallback.
rem   2. py -3, the Python launcher.
rem   3. Each python.exe on PATH, in PATH order. Empty and relative PATH
rem      entries are skipped, and the Microsoft Store alias fails the probe.
rem   4. Default locations: python.org per-user and all-users installs, the
rem      Python install manager, and pyenv-win.
rem
rem Constraints that keep this file working in hostile environments:
rem   - ASCII only, with no labels or GOTO, so it still parses with LF
rem     line endings.
rem   - Candidates are judged by exit code. Interpreter paths are never
rem     captured from program output, because for /f decodes output in the
rem     console code page and corrupts non-ASCII paths; only paths that cmd
rem     built itself are used.
rem   - The probe text contains no redirection or escape characters, so a
rem     dropped quote cannot turn it into a redirection.
rem   - The argument list appears only on the final top-level command line,
rem     never inside a parenthesized block or a for list, where an argument
rem     such as x)y or a^b would break parsing.
rem   - The final exit passes through CMD_EXIT, the TheKnowledge shim
rem     convention.
rem
rem Candidate TheKnowledge starter file: keep it project-neutral.
setlocal EnableExtensions DisableDelayedExpansion
set "NoDefaultCurrentDirectoryInExePath=1"
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "TK_STAGE1=%~dp0scripts\install-stage-1-windows.py"
set "TK_PROBE=import sys; raise SystemExit(not (sys.hexversion // 0x03090000 and sys.platform == 'win32'))"
set "TK_PY="
set "TK_PYARG="
set "TK_OVERRIDE="
set "TK_HELP="
if "%~1"=="-h" set "TK_HELP=1"
if "%~1"=="--help" set "TK_HELP=1"
if "%~1"=="/?" set "TK_HELP=1"
if not exist "%TK_STAGE1%" (
  >&2 echo install.cmd: missing "%TK_STAGE1%"; this checkout is incomplete.
  exit /b 1
)
if defined THEKNOWLEDGE_BOOTSTRAP_PYTHON set "TK_OVERRIDE=%THEKNOWLEDGE_BOOTSTRAP_PYTHON:"=%"
if defined TK_OVERRIDE (
  call "%TK_OVERRIDE%" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%TK_OVERRIDE%"
)
if defined TK_OVERRIDE if not defined TK_PY (
  >&2 echo install.cmd: THEKNOWLEDGE_BOOTSTRAP_PYTHON does not name a native Python 3.9+; not falling back.
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
  echo Usage: install.cmd [stage-2-options]
  echo.
  echo Install or bootstrap from this checkout.
  echo.
  echo Default behavior:
  echo   - normal user: user-local non-development install
  echo   - --mode dev, --mode venv-only, --system: not supported on native Windows
  echo.
  echo Options:
  echo   -h, --help, /?  Show this help, then the current stage-2 options when a
  echo                   usable Python interpreter is already available.
  echo.
  echo All remaining options are passed through to scripts/install-stage-2.py.
  echo.
  echo Stage-2 options are unavailable until a Python 3.9+ interpreter is present.
  echo Run scripts\install_prerequisites.cmd to install one.
  exit /b 0
)
if not defined TK_PY (
  >&2 echo install.cmd: no native Python 3.9+ found. Tried THEKNOWLEDGE_BOOTSTRAP_PYTHON,
  >&2 echo install.cmd: py -3, each python.exe on PATH, and the default install locations.
  >&2 echo install.cmd: The Microsoft Store "python" alias does not count.
  >&2 echo install.cmd: Run scripts\install_prerequisites.cmd to install Python, or set
  >&2 echo install.cmd: THEKNOWLEDGE_BOOTSTRAP_PYTHON to a python.exe, for example one from
  >&2 echo install.cmd: an Anaconda Prompt. Then open a new terminal and rerun install.cmd.
  exit /b 1
)
"%TK_PY%" %TK_PYARG% "%TK_STAGE1%" %*
set "CMD_EXIT=%ERRORLEVEL%"
exit /b %CMD_EXIT%
