# Сборка исходников RECOVERY EA в MetaEditor

Это **не установщик и не релиз для торговли**. Сборка работает в отдельной
временной папке, не запускает MT5, не подключает советники к графикам, не
отправляет ордера и не изменяет настройки или каталог данных терминала.

Нужны Git, Python 3.13 x64 и установленный официальный MetaEditor. Скачивать
исполняемые файлы этим скриптом нельзя. Пример ниже запускается в PowerShell
из корня локальной копии GitHub, содержащей эти инструменты и точный коммит
с проверяемыми исходниками.

```powershell
$Commit = '8b5e87fd36015a02e1541b07a996ac285dc51afe'
$Deploy = 'design\recovery-ea\deployment'
$Archive = Join-Path $env:TEMP 'recovery-ea-diagnostic.zip'
$Results = Join-Path $env:TEMP 'recovery-ea-compile-results'
$MetaEditor = 'C:\Program Files\MetaTrader 5\metaeditor64.exe'  # Укажи свой путь
$PackJson = py -3 "$Deploy\source_build.py" package --repo . --commit $Commit --out $Archive
if ($LASTEXITCODE -ne 0) { throw 'ZIP сборки не создан' }
$Pack = $PackJson | ConvertFrom-Json
& "$Deploy\Compile-RecoverySources.ps1" -MetaEditor $MetaEditor -Archive $Archive `
  -Commit $Commit -ArchiveSha256 $Pack.zip_sha256 -Results $Results
```

Для передачи ZIP с другого компьютера берите SHA-256 у доверенного
публикатора отдельно от ZIP. Не вычисляйте «ожидаемый хеш» из неизвестного
архива. Скрипт дополнительно проверяет коммит манифеста и SHA-256 каждого
исходника. При повторении задайте **новые** пути к ZIP и папке результата:
инструменты не перезаписывают существующие результаты.

По умолчанию собираются `RecoveryVerifier.mq5` и диагностический
`RecoveryPairProbe.mq5`. ZIP содержит только эти исходники и их локальные
зависимости из указанного коммита; не берёт файлы, которые изменены только
в рабочей папке. Для появившегося торгового EA потребуется явно выбрать
`--trader <путь относительно design/recovery-ea>` на этапе упаковки и
`-Mode candidate` на этапе компиляции. Режим candidate только маркирует
результат, но не разрешает установку, запускающие действия или торговлю.

Результат: `compile-report.json`, исходные логи компилятора и новые `.ex5`
только для успешно скомпилированных целей. Если результат неизвестен или
лог неполный, сборка считается неуспешной. Запускается только
[компиляция через MetaEditor](https://www.metatrader5.com/en/metaeditor/help/beginning/integration_ide);
выпуск требует отдельного прогона и проверки полного цикла на демо.
Перед передачей логов удалите личное имя из путей и лишние данные;
достаточно результата и строк с ошибками.

Проверка оболочки на Linux: `python -m unittest -v test_source_build.py`.
Она проверяет упаковку и имитирует MetaEditor, а нативную сборку Windows
не подтверждает.
