@echo off
echo 正在開放防火牆連接埠 8100（手機連線用）...
netsh advfirewall firewall show rule name="QR Tool 8100" >nul 2>&1
if %errorlevel%==0 (
    echo 防火牆規則已存在，不需重複新增。
    goto end
)
netsh advfirewall firewall add rule name="QR Tool 8100" dir=in action=allow protocol=TCP localport=8100
if %errorlevel%==0 (
    echo 完成！手機現在可以連線了。
) else (
    echo 失敗：請按右鍵「以系統管理員身分執行」此檔案。
)
:end
pause
