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
rem   - Interpreters are started without call, which would re-parse a path
rem     holding ^ or %; only py and an override that is not a .exe use it.
rem   - PATH is read only through delayed expansion, so it never lands on
rem     a parsed line: cmd aborts a batch file whose line grows past 8191
rem     characters, and crashes when a delayed replacement grows past that
rem     size. Delayed expansion yields nothing for a variable over 8191
rem     characters, so an empty first character marks such a PATH. A PATH
rem     whose quoted entry list would pass 8000 characters is not scanned;
rem     the other candidates still work. Quotes are removed first, so a
rem     quoted entry such as one under Program Files (x86) cannot break the
rem     for list. Each entry is tested with delayed expansion off again, so
rem     entries containing ! keep their spelling, and a hit leaves both
rem     scopes at once with "endlocal & endlocal & set".
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
set "TK_OVERRIDE_EXT="
if defined TK_OVERRIDE for %%P in ("%TK_OVERRIDE%") do set "TK_OVERRIDE_EXT=%%~xP"
if defined TK_OVERRIDE if /i "%TK_OVERRIDE_EXT%"==".exe" (
  "%TK_OVERRIDE%" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%TK_OVERRIDE%"
)
if defined TK_OVERRIDE if /i not "%TK_OVERRIDE_EXT%"==".exe" (
  call "%TK_OVERRIDE%" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%TK_OVERRIDE%"
)
if defined TK_OVERRIDE if not defined TK_PY (
  >&2 echo install.cmd: THEKNOWLEDGE_BOOTSTRAP_PYTHON does not name a native Python 3.9+; not falling back.
  >&2 echo install.cmd: Name a python.exe; a path with ^^ or %% works only for a .exe file.
  exit /b 1
)
if not defined TK_PY (
  call py -3 -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=py" && set "TK_PYARG=-3"
)
set "TK_PATH="
set "TK_LONGPATH="
if not defined TK_PY setlocal EnableDelayedExpansion
if not defined TK_PY if defined PATH if "!PATH:~0,1!"=="" set "TK_LONGPATH=1"
if not defined TK_PY if defined PATH if not "!PATH:~8000,1!"=="" set "TK_LONGPATH=1"
if not defined TK_PY if defined PATH if not defined TK_LONGPATH set "TK_PATH=!PATH:"=!"
if defined TK_PATH (
  set "TK_A=!TK_PATH!#"
  set "TK_B=!TK_PATH:;=!#"
  set /a "TK_LA=0, TK_LB=0"
  for %%N in (4096 2048 1024 512 256 128 64 32 16 8 4 2 1) do (
    if not "!TK_A:~%%N,1!"=="" (set /a "TK_LA+=%%N" & set "TK_A=!TK_A:~%%N!")
    if not "!TK_B:~%%N,1!"=="" (set /a "TK_LB+=%%N" & set "TK_B=!TK_B:~%%N!")
  )
  set /a "TK_LISTLEN=3*TK_LA-2*TK_LB"
)
if defined TK_PATH if !TK_LISTLEN! gtr 8000 (set "TK_PATH=" & set "TK_LONGPATH=1")
if not defined TK_PY if defined TK_PATH for %%D in ("!TK_PATH:;=" "!") do if not defined TK_PY (
  setlocal DisableDelayedExpansion
  if not "%%~D"=="" if /i "%%~fD"=="%%~D" if exist "%%~D\python.exe" (
    "%%~D\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && (endlocal & endlocal & set "TK_PY=%%~D\python.exe")
  )
  if not defined TK_PY endlocal
)
if not defined TK_PY (endlocal & set "TK_LONGPATH=%TK_LONGPATH%")
if not defined TK_PY if defined LOCALAPPDATA for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY if defined LOCALAPPDATA for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY if defined ProgramFiles for /d %%D in ("%ProgramFiles%\Python3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
)
if not defined TK_PY if defined USERPROFILE for /d %%D in ("%USERPROFILE%\.pyenv\pyenv-win\versions\3*") do if not defined TK_PY if exist "%%~fD\python.exe" (
  "%%~fD\python.exe" -c "%TK_PROBE%" 1>nul 2>nul && set "TK_PY=%%~fD\python.exe"
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
  if defined TK_LONGPATH >&2 echo install.cmd: PATH was not scanned; it is too long for cmd to split.
  >&2 echo install.cmd: The Microsoft Store "python" alias does not count.
  >&2 echo install.cmd: Run scripts\install_prerequisites.cmd to install Python, or set
  >&2 echo install.cmd: THEKNOWLEDGE_BOOTSTRAP_PYTHON to a python.exe, for example one from
  >&2 echo install.cmd: an Anaconda Prompt. Then open a new terminal and rerun install.cmd.
  exit /b 1
)
"%TK_PY%" %TK_PYARG% "%TK_STAGE1%" %*
set "CMD_EXIT=%ERRORLEVEL%"
exit /b %CMD_EXIT%
