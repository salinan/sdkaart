@echo off
echo ====================================
echo  SD Kaart Manager - Build naar .exe
echo ====================================

:: Installeer dependencies
pip install customtkinter psutil pyinstaller

:: Bouw de .exe
pyinstaller ^
  --onefile ^
  --windowed ^
  --name "SD_Kaart_Manager" ^
  --add-data "sd_manager_config.json;." ^
  sd_manager.py

echo.
echo Klaar! De .exe staat in de "dist" map.
pause
