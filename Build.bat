@echo off
cd /d "%~dp0"
python -m PyInstaller --noconfirm --clean --onefile --windowed --name pic-click --distpath package-staging --workpath build\package --paths .vendor --collect-all pystray app.py
if errorlevel 1 goto :error
if not exist dist mkdir dist
copy /Y package-staging\pic-click.exe dist\pic-click.exe >nul
if errorlevel 1 goto :error
echo Build complete: dist\pic-click.exe
exit /b 0
:error
echo Build failed. Close any running pic-click.exe and retry.
pause
exit /b 1
