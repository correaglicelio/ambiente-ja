@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Coletando focos de calor (INPE) - Mato Grosso...
python scripts\coletar_focos.py --uf MT
pause
