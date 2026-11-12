@echo off
setlocal
cd /d "%~dp0"
set "PYTHON="

rem Prefer the bundled portable Python. Never fall back to a system installation.
for %%P in (
    "%~dp0python\python.exe"
    "%~dp0Python\python.exe"
    "%~dp0runtime\python.exe"
    "%~dp0python.exe"
) do (
    if not defined PYTHON if exist "%%~P" set "PYTHON=%%~fP"
)

rem Also support a bundled Python placed in another subfolder.
if not defined PYTHON (
    for /r "%~dp0" %%P in (python.exe) do (
        if not defined PYTHON set "PYTHON=%%~fP"
    )
)

if not defined PYTHON (
    echo Bundled python.exe was not found inside the RefSynapse folder.
    pause
    exit /b 1
)

echo Using portable Python: "%PYTHON%"
"%PYTHON%" -c "import PySide6, numpy, numba, trimesh, lxml, assimp_py, cadquery" >nul 2>nul
if errorlevel 1 (
    echo Installing RefSynapse dependencies into the bundled Python...
    echo This can take several minutes on the first 3D-enabled launch.
    "%PYTHON%" -m pip install --no-user --disable-pip-version-check -r requirements.txt
    if errorlevel 1 (
        echo.
        echo Could not install the required dependencies automatically.
        echo If this is an embeddable Python, enable "import site" in its ._pth file
        echo and install pip/PySide6 into this same portable Python.
        pause
        exit /b 1
    )
)

for %%P in ("%PYTHON%") do set "PYTHONW=%%~dpPpythonw.exe"
if exist "%PYTHONW%" (
    rem GUI mode: close this console instead of keeping python.exe attached to it.
    start "" /D "%~dp0" "%PYTHONW%" "%~dp0RefSynapse.py"
    exit /b 0
)

rem Fallback for portable distributions that do not include pythonw.exe.
"%PYTHON%" RefSynapse.py
if errorlevel 1 pause
endlocal
