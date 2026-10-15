@echo off
rem Rebuilds the app from modpack_server_builder.py. Needs Python 3.10+ (64-bit) from python.org.
cd /d "%~dp0"
set PY=python
where py >nul 2>nul && set PY=py -3
%PY% -m pip install --upgrade pyinstaller || goto :fail
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name "Modpack Server Builder" --icon assets\app.ico --add-binary "vendor\webview.dll;." --add-data "assets\app.ico;." --exclude-module tkinter --exclude-module unittest --exclude-module pydoc modpack_server_builder.py || goto :fail
copy /y "dist\Modpack Server Builder.exe" "dist\Modpack Server Builder Setup.exe" >nul
echo.
echo Done: dist\Modpack Server Builder Setup.exe  (installer)  and  dist\Modpack Server Builder.exe  (portable)
pause
exit /b 0
:fail
echo Build failed - see the messages above.
pause
exit /b 1
