using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class RecoverySetup
{
    private const string Version = "254e6932";
    private const string OwnerDir = ".recovery-setup-owned";
    private const string Partial = ".recovery-setup-writing";
    private static readonly string[] Experts = {
        "RecoveryTrader.ex5", "RecoveryVerifier.ex5", "RecoveryPriceWatcher.ex5"
    };
    private static readonly StringComparer WindowsNames = StringComparer.OrdinalIgnoreCase;

    private static string Digest(Stream stream)
    {
        using (var sha = SHA256.Create())
            return BitConverter.ToString(sha.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
    }

    private static FileAttributes? Attributes(string path)
    {
        try { return File.GetAttributes(path); }
        catch (FileNotFoundException) { return null; }
        catch (DirectoryNotFoundException) { return null; }
    }

    // Check existing ancestors as well as the leaf. Directory.Exists alone
    // cannot distinguish a missing path from a broken link or access failure.
    private static FileAttributes? Plain(string path)
    {
        string parent = Path.GetDirectoryName(path);
        if (parent != null) Plain(parent);
        FileAttributes? attr = Attributes(path);
        if (attr.HasValue && (attr.Value & FileAttributes.ReparsePoint) != 0)
            throw new IOException("Путь содержит ссылку или точку повторной обработки: " + path);
        return attr;
    }

    private static bool IsDirectory(string path)
    {
        var attr = Plain(path);
        if (!attr.HasValue) return false;
        if ((attr.Value & FileAttributes.Directory) == 0)
            throw new IOException("Вместо папки обнаружен файл: " + path);
        return true;
    }

    private static string LocalPath(string path)
    {
        if (String.IsNullOrWhiteSpace(path) || !Regex.IsMatch(path, @"^[a-zA-Z]:[\\/]"))
            throw new ArgumentException("Укажите полный локальный путь на диске Windows.");
        string full = Path.GetFullPath(path);
        if (full.Length > 3) full = full.TrimEnd('\\', '/');
        Plain(full);
        return full;
    }

    private static void MakeDirectory(string path)
    {
        if (IsDirectory(path)) return;
        MakeDirectory(Path.GetDirectoryName(path));
        Directory.CreateDirectory(path);
        if (!IsDirectory(path)) throw new IOException("Не удалось создать папку: " + path);
    }

    private static void SafeRelative(string path)
    {
        foreach (string part in path.Split('/'))
        {
            if (part.Length == 0 || part.EndsWith(".") || part.EndsWith(" ") ||
                part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 ||
                Regex.IsMatch(part, @"^(CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(\.|$)",
                    RegexOptions.IgnoreCase | RegexOptions.CultureInvariant) ||
                WindowsNames.Equals(part, Partial) || WindowsNames.Equals(part, OwnerDir))
                throw new InvalidDataException("Недопустимое имя в пакете: " + path);
        }
    }

    private sealed class Item
    {
        internal string Hash;
        internal ZipArchiveEntry Entry;
    }

    private static Dictionary<string, Item> ReadPackage(ZipArchive archive)
    {
        var files = new Dictionary<string, ZipArchiveEntry>(WindowsNames);
        var dirs = new HashSet<string>(WindowsNames);
        var explicitDirs = new HashSet<string>(WindowsNames);
        foreach (ZipArchiveEntry entry in archive.Entries)
        {
            string name = entry.FullName;
            bool directory = name.EndsWith("/", StringComparison.Ordinal);
            if (directory) name = name.Substring(0, name.Length - 1);
            SafeRelative(name);
            uint attrs = unchecked((uint)entry.ExternalAttributes);
            uint type = (attrs >> 16) & 0xf000;
            if ((attrs & (uint)FileAttributes.ReparsePoint) != 0 ||
                (type != 0 && type != (directory ? 0x4000u : 0x8000u)))
                throw new InvalidDataException("Недопустимый тип записи ZIP: " + name);
            if (directory)
            {
                if (entry.Length != 0 || !explicitDirs.Add(name))
                    throw new InvalidDataException("Повторная или непустая запись каталога ZIP: " + name);
                continue;
            }
            if (files.ContainsKey(name)) throw new InvalidDataException("Дублирующийся файл ZIP: " + name);
            files.Add(name, entry);
            int slash = name.LastIndexOf('/');
            while (slash >= 0)
            {
                dirs.Add(name.Substring(0, slash));
                slash = name.LastIndexOf('/', slash - 1);
            }
        }
        if (!explicitDirs.IsSubsetOf(dirs) || dirs.Overlaps(files.Keys))
            throw new InvalidDataException("Недопустимая структура каталогов ZIP.");
        ZipArchiveEntry manifest;
        if (!files.TryGetValue("SHA256SUMS", out manifest) || manifest.FullName != "SHA256SUMS")
            throw new InvalidDataException("В пакете отсутствует SHA256SUMS.");
        var result = new Dictionary<string, Item>(StringComparer.Ordinal);
        var paths = new HashSet<string>(WindowsNames);
        using (var reader = new StreamReader(manifest.Open(), new UTF8Encoding(false, true), false))
        {
            string line;
            while ((line = reader.ReadLine()) != null)
            {
                Match m = Regex.Match(line, @"\A([0-9a-f]{64})  (.+)\z");
                if (!m.Success) throw new InvalidDataException("Повреждена строка SHA256SUMS.");
                string name = m.Groups[2].Value;
                ZipArchiveEntry entry;
                if (!paths.Add(name) || !files.TryGetValue(name, out entry) || entry.FullName != name ||
                    !(name.StartsWith("App/", StringComparison.Ordinal) ||
                      Experts.Any(x => name == "Experts/" + x)))
                    throw new InvalidDataException("Недопустимый или отсутствующий файл манифеста: " + name);
                string hash = m.Groups[1].Value;
                using (Stream contents = entry.Open())
                    if (Digest(contents) != hash) throw new InvalidDataException("Ошибка SHA-256 пакета: " + name);
                result.Add(name, new Item { Hash = hash, Entry = entry });
            }
        }
        if (result.Count + 1 != files.Count || !result.ContainsKey("App/stage-manifest.json") ||
            Experts.Any(x => !result.ContainsKey("Experts/" + x) || result["Experts/" + x].Entry.Length == 0))
            throw new InvalidDataException("Состав пакета не соответствует SHA256SUMS или отсутствует одна из трёх ролей.");
        return result;
    }

    private sealed class InstallTree
    {
        private readonly string destination, stage, backup;
        private readonly Dictionary<string, Item> expected;
        private readonly HashSet<string> directories = new HashSet<string>(WindowsNames);

        internal InstallTree(string target, string workRoot, string prefix, Dictionary<string, Item> package)
        {
            destination = target;
            // Working EA files stay outside Experts until all three hashes match.
            string stem = ".ai-impuls-setup-" + Version + "-" + prefix.TrimEnd('/');
            stage = Path.Combine(workRoot, stem + "-stage");
            backup = Path.Combine(workRoot, stem + "-previous");
            expected = package.Where(x => x.Key.StartsWith(prefix, StringComparison.Ordinal)).ToDictionary(
                x => x.Key.Substring(prefix.Length).Replace('/', '\\'), x => x.Value, WindowsNames);
            foreach (string file in expected.Keys)
                for (string dir = Path.GetDirectoryName(file); !String.IsNullOrEmpty(dir); dir = Path.GetDirectoryName(dir))
                    directories.Add(dir);
        }

        private bool VerifyFile(string path, Item item)
        {
            Plain(path);
            using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read))
                return stream.Length == item.Entry.Length && Digest(stream) == item.Hash;
        }

        // A missing tree is a valid new target. Any foreign or changed file
        // stops the installation, including during recovery from interruption.
        private bool Inspect(string root, bool preparing = false)
        {
            if (!IsDirectory(root)) return false;
            var pending = new Stack<string>();
            pending.Push(root);
            int count = 0;
            while (pending.Count > 0)
                foreach (string path in Directory.GetFileSystemEntries(pending.Pop()))
                {
                    string relative = path.Substring(root.Length + 1);
                    FileAttributes? attr = Plain(path);
                    if (!attr.HasValue) throw new IOException("Файл исчез во время проверки: " + path);
                    bool dir = (attr.Value & FileAttributes.Directory) != 0;
                    if (preparing && WindowsNames.Equals(relative, OwnerDir) && dir &&
                        Directory.GetFileSystemEntries(path).Length == 0) continue;
                    if (preparing && WindowsNames.Equals(relative, Partial) && !dir &&
                        IsDirectory(Path.Combine(root, OwnerDir))) continue;
                    if (dir && directories.Contains(relative)) { pending.Push(path); continue; }
                    Item item;
                    if (dir || !expected.TryGetValue(relative, out item))
                        throw new IOException("Обнаружен посторонний файл или каталог: " + path);
                    if (!VerifyFile(path, item))
                        throw new IOException("Существующий файл отличается от проверенной сборки: " + path);
                    count++;
                }
            return count == expected.Count;
        }

        private void DeleteVerifiedBackup()
        {
            if (!IsDirectory(backup)) return;
            Inspect(backup);
            foreach (var file in expected)
            {
                string path = Path.Combine(backup, file.Key);
                if (!Plain(path).HasValue) continue;
                if (!VerifyFile(path, file.Value)) throw new IOException("Резервная копия была изменена: " + path);
                File.Delete(path);
            }
            foreach (string relative in directories.OrderByDescending(x => x.Length))
            {
                string path = Path.Combine(backup, relative);
                if (IsDirectory(path)) Directory.Delete(path, false);
            }
            Directory.Delete(backup, false);
        }

        internal void Check()
        {
            bool full = Inspect(destination);
            Inspect(stage, true);
            if (IsDirectory(backup))
            {
                Inspect(backup);
                if (IsDirectory(destination) && !full)
                    throw new IOException("Требуется проверка двух неполных каталогов: " + destination + " и " + backup);
            }
        }

        internal void Prepare()
        {
            if (IsDirectory(backup))
            {
                if (!IsDirectory(destination)) Directory.Move(backup, destination);
                else DeleteVerifiedBackup();
            }
            if (Inspect(destination)) return;
            MakeDirectory(stage);
            Inspect(stage, true);
            string marker = Path.Combine(stage, OwnerDir);
            MakeDirectory(marker);
            string partial = Path.Combine(stage, Partial);
            if (Plain(partial).HasValue) File.Delete(partial);
            foreach (var file in expected)
            {
                string path = Path.Combine(stage, file.Key);
                if (Plain(path).HasValue) continue; // already verified by Inspect
                MakeDirectory(Path.GetDirectoryName(path));
                using (var output = new FileStream(partial, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                using (var input = file.Value.Entry.Open())
                {
                    input.CopyTo(output);
                    output.Flush(true);
                }
                if (!VerifyFile(partial, file.Value)) throw new IOException("Ошибка проверки копии: " + file.Key);
                File.Move(partial, path);
            }
            Directory.Delete(marker, false);
            if (!Inspect(stage)) throw new IOException("Подготовлен неполный каталог: " + stage);
        }

        internal void Publish()
        {
            if (Inspect(destination)) return;
            if (!Inspect(stage)) throw new IOException("Комплект для установки неполон.");
            bool moved = false;
            if (IsDirectory(destination))
            {
                Directory.Move(destination, backup);
                moved = true;
            }
            try
            {
                if (moved) Inspect(backup);
                // Directory.Move publishes a whole verified tree on its own volume.
                Directory.Move(stage, destination);
            }
            catch
            {
                if (moved && !IsDirectory(destination)) Directory.Move(backup, destination);
                throw;
            }
            if (!Inspect(destination)) throw new IOException("Итоговая проверка не пройдена: " + destination);
            DeleteVerifiedBackup();
        }
    }

    private sealed class SetupLock : IDisposable
    {
        private readonly Mutex mutex;
        internal SetupLock(string path)
        {
            string id;
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(path.ToUpperInvariant())))
                id = Digest(stream);
            mutex = new Mutex(false, @"Global\AI-IMPULS-RECOVERY-Setup-" + id);
            try
            {
                if (!mutex.WaitOne(0)) throw new IOException("Установка для этого каталога уже выполняется.");
            }
            catch (AbandonedMutexException) { /* we own the lock after process termination */ }
            catch { mutex.Dispose(); throw; }
        }
        public void Dispose() { mutex.ReleaseMutex(); mutex.Dispose(); }
    }

    private static string Install(string selected)
    {
        if (!Environment.Is64BitOperatingSystem) throw new PlatformNotSupportedException("Требуется Windows x64.");
        string root = LocalPath(selected);
        string experts = Path.Combine(root, "MQL5", "Experts");
        if (!IsDirectory(experts))
            throw new DirectoryNotFoundException("Выберите каталог данных MT5, содержащий MQL5\\Experts.");
        string local = LocalPath(Environment.GetEnvironmentVariable("LOCALAPPDATA"));
        string versions = Path.Combine(local, "AI-IMPULS-RECOVERY", "versions");
        string app = Path.Combine(versions, Version);
        string eas = Path.Combine(experts, "RECOVERY");
        using (var resource = Assembly.GetExecutingAssembly().GetManifestResourceStream("RecoveryPayload.zip"))
        {
            if (resource == null) throw new InvalidDataException("В установщике отсутствует RecoveryPayload.zip.");
            using (var zip = new ZipArchive(resource, ZipArchiveMode.Read))
            {
                var contents = ReadPackage(zip);
                using (new SetupLock(app))
                using (new SetupLock(eas))
                {
                    var application = new InstallTree(app, versions, "App/", contents);
                    var advisors = new InstallTree(eas, root, "Experts/", contents);
                    application.Check();
                    advisors.Check();
                    MakeDirectory(versions);
                    application.Prepare();
                    advisors.Prepare();
                    application.Publish();
                    advisors.Publish();
                }
            }
        }
        return "Три советника проверены и установлены в " + eas + Environment.NewLine +
               "Руководство: " + Path.Combine(app, "README_RU.txt") + Environment.NewLine +
               "Запуск панели: " + Path.Combine(app, "RECOVERY.cmd");
    }

    private static IEnumerable<string> FindTerminals()
    {
        var found = new List<string>();
        string roaming = Environment.GetEnvironmentVariable("APPDATA");
        if (String.IsNullOrEmpty(roaming)) return found;
        try
        {
            string root = Path.Combine(LocalPath(roaming), "MetaQuotes", "Terminal");
            if (IsDirectory(root))
                foreach (string path in Directory.GetDirectories(root))
                {
                    try
                    {
                        if (IsDirectory(Path.Combine(path, "MQL5", "Experts")) &&
                            Plain(Path.Combine(path, "origin.txt")).HasValue) found.Add(path);
                    }
                    catch (IOException) { }
                    catch (UnauthorizedAccessException) { }
                }
        }
        catch (IOException) { }
        catch (UnauthorizedAccessException) { }
        return found.OrderBy(x => x, WindowsNames);
    }

    private static Form SetupWindow()
    {
        var form = new Form {
            Text = "Установка AI IMPULS RECOVERY", StartPosition = FormStartPosition.CenterScreen,
            ClientSize = new Size(690, 230), MinimumSize = new Size(610, 260),
            AutoScaleMode = AutoScaleMode.Dpi, Font = SystemFonts.MessageBoxFont
        };
        var instructions = new Label { AutoSize = true, Text = "Каталог данных MT5 (Файл → Открыть каталог данных)", Dock = DockStyle.Fill };
        var paths = new ComboBox { Dock = DockStyle.Fill, DropDownStyle = ComboBoxStyle.DropDown };
        paths.Items.AddRange(FindTerminals().Cast<object>().ToArray());
        if (paths.Items.Count == 1) paths.SelectedIndex = 0;
        var browse = new Button { Text = "Обзор…", AutoSize = true };
        browse.Click += delegate {
            using (var dialog = new FolderBrowserDialog { Description = instructions.Text, ShowNewFolderButton = false })
                if (dialog.ShowDialog(form) == DialogResult.OK) paths.Text = dialog.SelectedPath;
        };
        var start = new Button { Text = "Установить", AutoSize = true, Anchor = AnchorStyles.Left };
        var status = new TextBox { Multiline = true, ReadOnly = true, Dock = DockStyle.Fill, ScrollBars = ScrollBars.Vertical,
            Text = "В выбранный терминал будут установлены три советника. Панель и руководство будут размещены в профиле Windows." };
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(14), ColumnCount = 2, RowCount = 4 };
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100));
        layout.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
        for (int i = 0; i < 3; i++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
        layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
        layout.Controls.Add(instructions, 0, 0); layout.SetColumnSpan(instructions, 2);
        layout.Controls.Add(paths, 0, 1); layout.Controls.Add(browse, 1, 1);
        layout.Controls.Add(start, 0, 2); layout.Controls.Add(status, 0, 3); layout.SetColumnSpan(status, 2);
        form.Controls.Add(layout);
        start.Click += async delegate {
            string selection = paths.Text;
            paths.Enabled = browse.Enabled = start.Enabled = false;
            status.Text = "Проверка комплекта и установка…";
            try { status.Text = await Task.Run(() => Install(selection)); }
            catch (Exception ex) { status.Text = "Установка не завершена. " + ex.Message; }
            finally { paths.Enabled = browse.Enabled = start.Enabled = true; }
        };
        return form;
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args.Length == 0)
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                using (Form form = SetupWindow()) Application.Run(form);
                return 0;
            }
            if (args.Length == 1 && args[0] == "--help")
            {
                Console.WriteLine("RecoverySetup.exe [--install <каталог данных MT5> | --help]");
                return 0;
            }
            if (args.Length != 2 || args[0] != "--install")
                throw new ArgumentException("Использование: RecoverySetup.exe --install <каталог данных MT5>");
            Console.WriteLine(Install(args[1]));
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine("Ошибка установки: " + ex.Message);
            return 1;
        }
    }
}
