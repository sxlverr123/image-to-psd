@echo off
setlocal EnableDelayedExpansion

set "EXIT_CODE=0"
echo installing dependencies

:: Find Python (python or py -3 launcher)
set "PY_CMD="
python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY_CMD=python"

if not defined PY_CMD (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY_CMD=py -3"
)

if not defined PY_CMD (
    echo ERROR: Python 3.8 or newer is required ^(Python not found^).
    set "EXIT_CODE=1"
    goto :end
)

:: Print version and verify Python >= 3.8
%PY_CMD% -c "import sys; print('Python ' + sys.version.split()[0] + '  (' + sys.executable + ')'); sys.exit(0 if sys.version_info >= (3, 8) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.8 or newer is required.
    set "EXIT_CODE=1"
    goto :end
)

:: Required packages
echo.
echo ^>^>^> pip install pillow numpy
%PY_CMD% -m pip install --upgrade pillow numpy
if errorlevel 1 (
    echo.
    echo Could not install the required packages. Check your internet connection.
    set "EXIT_CODE=1"
    goto :end
)

:: Recommended packages
set "FAILED="
for %%P in (opencv-python scikit-learn) do (
    echo.
    echo ^>^>^> pip install %%P
    %PY_CMD% -m pip install --upgrade %%P
    if errorlevel 1 (
        if defined FAILED (
            set "FAILED=!FAILED!, %%P"
        ) else (
            set "FAILED=%%P"
        )
    )
)

:: Optional package (rembg)
echo.
set "ANS="
set /p "ANS=Install rembg too (better subject cut-out, ~200 MB plus a model download on first use)? [y/N] "
if /i "%ANS%"=="y" goto :install_rembg
if /i "%ANS%"=="yes" goto :install_rembg
goto :check_tk

:install_rembg
echo.
echo ^>^>^> pip install rembg[cpu]
%PY_CMD% -m pip install --upgrade "rembg[cpu]"
if errorlevel 1 (
    if defined FAILED (
        set "FAILED=!FAILED!, rembg[cpu]"
    ) else (
        set "FAILED=rembg[cpu]"
    )
)

:: Check tkinter
:check_tk
%PY_CMD% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo.
    echo tkinter: MISSING - the window cannot open, but the command line still works.
    echo   Windows: reinstall Python from python.org and keep 'tcl/tk' ticked.
) else (
    echo.
    echo tkinter: OK ^(the window will work^)
)

if defined FAILED (
    echo.
    echo These optional packages failed to install: %FAILED%
    echo PSD Maker still works, with fewer features.
)

echo.
echo All set! Double-click psd_maker.py to start.

:end
echo.
set /p "DUMMY=Press Enter to close..."
exit /b %EXIT_CODE%
