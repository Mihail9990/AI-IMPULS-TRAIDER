# Сборка установщика RECOVERY для Windows x64

Установщик содержит три EX5, полученные в MetaEditor из `254e6932e9898b20315f003d498fe598d8e70baa`, локальную панель и официальный встраиваемый CPython 3.13.17. Установщику не нужны доступ к сети или отдельная установка Python. Метаданные лицензий поставляются вместе с интерпретатором и библиотеками.

## Восстановить точный коммит советников

В новой ветке опубликованы исходники и полный Git-снимок двух локальных коммитов советников относительно `f39a50a3893ee08e063113ce94a43aecb713f843`. Чтобы восстановить их в клоне этого репозитория, содержащем исходный коммит, выполните из корня:

```powershell
git bundle verify releases/RECOVERY-254e6932/EA-source-254e6932.bundle
git fetch releases/RECOVERY-254e6932/EA-source-254e6932.bundle refs/heads/fix/recovery-metaeditor-6327
git rev-parse FETCH_HEAD
```

Проверьте, что `FETCH_HEAD` равен полному SHA советников выше. Подготовьте candidate-src.zip и compile-results по [инструкции сборки трёх EA](../SOURCE_BUILD_RU.md) с этим точным SHA. Для сборки оболочки оставайтесь в ветке установщика.

## Собрать EXE

На машине сборки нужны Windows x64, Python 3.13 x64 с pip и системный компилятор .NET Framework x64. Символы `$archive`, `$sha` и `$results` ниже означают путь к candidate-src.zip, SHA-256, выданный упаковщиком исходников, и каталог результатов MetaEditor. Выберите новый путь для выходного EXE.

```powershell
py -3.13 design/recovery-ea/deployment/installer/build_installer.py --archive $archive --archive-sha256 $sha --results $results --cache .\setup-cache --output .\out\RECOVERY-Setup-x64.exe
py -3.13 design/recovery-ea/deployment/installer/test_installer.py --setup .\out\RECOVERY-Setup-x64.exe
```

Сборщик проверяет связность EX5, отчёта и исходников штатным `prepare_stage`. При первом запуске он скачивает [официальный Python](https://www.python.org/downloads/release/python-31317/) и колёса PyPI с закреплёнными SHA-256. Готовый кэш позволяет собрать пакет офлайн. В состав вложенного ZIP входят только перечисленные при сборке файлы. Мастер проверяет целостность по SHA-256 перед установкой. Расположение установленного приложения — `%LOCALAPPDATA%\AI-IMPULS-RECOVERY\versions\254e6932`.

Для установки без графического интерфейса запустите EXE с аргументами `--install "полный путь к каталогу данных MT5"` и дождитесь завершения процесса. Код 0 означает успешную установку или уже установленный совпадающий комплект. По `--help` доступна справка.
