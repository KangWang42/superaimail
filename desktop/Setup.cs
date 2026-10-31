using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Reflection;
using System.Windows.Forms;
using Microsoft.Win32;

namespace SuperAIMailInstaller {
static class Setup {
    const string Name = "SuperAIMail", Version = "0.1.0";
    static readonly bool Sandbox = Environment.GetEnvironmentVariable("SUPERMAIL_SETUP_SANDBOX") == "1";
    static string Key { get { return @"Software\Microsoft\Windows\CurrentVersion\Uninstall\SuperAIMail" + (Sandbox ? "-Sandbox" : ""); } }
    static string Link { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), Name + (Sandbox ? " sandbox" : "") + ".lnk"); } }
    static string Target {
        get { using (var key = Registry.CurrentUser.OpenSubKey(Key)) return key == null ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", Name) : Convert.ToString(key.GetValue("InstallLocation")); }
    }

    static void Shortcut(string target) {
        Type type = Type.GetTypeFromProgID("WScript.Shell");
        object shell = Activator.CreateInstance(type);
        object link = type.InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { Link });
        Type kind = link.GetType();
        kind.InvokeMember("TargetPath", BindingFlags.SetProperty, null, link, new object[] { Path.Combine(target, Name + ".exe") });
        kind.InvokeMember("WorkingDirectory", BindingFlags.SetProperty, null, link, new object[] { target });
        kind.InvokeMember("Save", BindingFlags.InvokeMethod, null, link, null);
    }

    static void CheckStopped(string target) {
        foreach (var process in Process.GetProcessesByName(Name)) {
            using (process) {
                try { if (process.MainModule.FileName.StartsWith(target + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new IOException("请先关闭正在运行的 SuperAIMail。"); }
                catch (System.ComponentModel.Win32Exception) { }
            }
        }
    }

    static void Install(string target) {
        target = Path.GetFullPath(target).TrimEnd(Path.DirectorySeparatorChar);
        if (Directory.Exists(target) && Directory.EnumerateFileSystemEntries(target).Any() && !File.Exists(Path.Combine(target, "superaimail-install.txt")))
            throw new IOException("请选择空文件夹，或已有的 SuperAIMail 安装目录。");
        CheckStopped(target);
        Directory.CreateDirectory(target);
        using (var payload = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (var zip = new ZipArchive(payload, ZipArchiveMode.Read)) {
            string root = target + Path.DirectorySeparatorChar;
            foreach (var entry in zip.Entries) {
                string path = Path.GetFullPath(Path.Combine(target, entry.FullName));
                if (!path.StartsWith(root, StringComparison.OrdinalIgnoreCase)) throw new IOException("安装包包含无效路径。");
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                entry.ExtractToFile(path, true);
            }
            File.WriteAllLines(Path.Combine(target, "superaimail-install.txt"), zip.Entries.Select(e => e.FullName).Concat(new[] { "Uninstall.exe" }));
        }
        File.Copy(Application.ExecutablePath, Path.Combine(target, "Uninstall.exe"), true);
        Shortcut(target);
        using (var key = Registry.CurrentUser.CreateSubKey(Key)) {
            key.SetValue("DisplayName", Name + (Sandbox ? " sandbox" : ""));
            key.SetValue("DisplayVersion", Version);
            key.SetValue("Publisher", "KangWang42");
            key.SetValue("InstallLocation", target);
            key.SetValue("UninstallString", "\"" + Path.Combine(target, "Uninstall.exe") + "\" --uninstall");
            key.SetValue("DisplayIcon", Path.Combine(target, Name + ".exe"));
            key.SetValue("NoModify", 1); key.SetValue("NoRepair", 1);
        }
    }

    static void Uninstall(string target) {
        target = Path.GetFullPath(target).TrimEnd(Path.DirectorySeparatorChar);
        CheckStopped(target);
        string manifest = Path.Combine(target, "superaimail-install.txt");
        if (!File.Exists(manifest)) throw new IOException("安装清单缺失，无法安全卸载。");
        foreach (string name in File.ReadAllLines(manifest)) {
            string path = Path.GetFullPath(Path.Combine(target, name));
            if (!path.StartsWith(target + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase)) throw new IOException("安装清单路径无效。");
            if (String.Equals(path, Application.ExecutablePath, StringComparison.OrdinalIgnoreCase)) continue;
            if (File.Exists(path)) File.Delete(path);
        }
        File.Delete(manifest);
        foreach (string path in Directory.GetDirectories(target, "*", SearchOption.AllDirectories).OrderByDescending(p => p.Length))
            if (!Directory.EnumerateFileSystemEntries(path).Any()) Directory.Delete(path);
        if (File.Exists(Link)) File.Delete(Link);
        Registry.CurrentUser.DeleteSubKeyTree(Key, false);
        // The running uninstaller is removed by a hidden helper after this process exits.
        string escaped = Application.ExecutablePath.Replace("'", "''"), folder = target.Replace("'", "''");
        string script = "Wait-Process -Id " + Process.GetCurrentProcess().Id + " -ErrorAction SilentlyContinue; Remove-Item -LiteralPath '" + escaped + "' -Force; if (!(Get-ChildItem -LiteralPath '" + folder + "' -Force)) { Remove-Item -LiteralPath '" + folder + "' }";
        string encoded = Convert.ToBase64String(System.Text.Encoding.Unicode.GetBytes(script));
        Process.Start(new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile -NonInteractive -EncodedCommand " + encoded) { UseShellExecute = false, CreateNoWindow = true });
    }

    [STAThread]
    static int Main(string[] args) {
        Application.EnableVisualStyles();
        bool quiet = args.Contains("--quiet"), remove = args.Contains("--uninstall");
        string target = Target;
        int index = Array.IndexOf(args, "--dir");
        if (index >= 0 && index + 1 < args.Length) target = args[index + 1];
        try {
            if (remove) {
                if (!quiet && MessageBox.Show("卸载应用？邮箱数据和设置会保留。", Name, MessageBoxButtons.OKCancel) != DialogResult.OK) return 0;
                Uninstall(target);
            } else {
                if (!quiet) {
                    using (var form = new Form { Text = "安装 " + Name, ClientSize = new Size(520, 224), StartPosition = FormStartPosition.CenterScreen, FormBorderStyle = FormBorderStyle.FixedDialog, MaximizeBox = false, Font = new Font("Microsoft YaHei UI", 10f), Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath) }) {
                        var title = new Label { Text = "SuperAIMail " + Version, Font = new Font(form.Font.FontFamily, 18f, FontStyle.Bold), Left = 24, Top = 20, Width = 470, Height = 34 };
                        var note = new Label { Text = "多邮箱与 AI 工作台。无需管理员权限，数据保存在个人目录。", Left = 24, Top = 66, Width = 470, Height = 26 };
                        var path = new TextBox { Text = target, Left = 24, Top = 104, Width = 470 };
                        var install = new Button { Text = "安装", Left = 290, Top = 164, Width = 98, Height = 34, DialogResult = DialogResult.OK };
                        var cancel = new Button { Text = "取消", Left = 400, Top = 164, Width = 94, Height = 34, DialogResult = DialogResult.Cancel };
                        form.Controls.AddRange(new Control[] { title, note, path, install, cancel }); form.AcceptButton = install; form.CancelButton = cancel;
                        if (form.ShowDialog() != DialogResult.OK) return 0;
                        target = path.Text;
                    }
                }
                Install(target);
                if (!quiet) Process.Start(Path.Combine(target, Name + ".exe"));
            }
            return 0;
        } catch (Exception error) {
            if (!quiet) MessageBox.Show(error.Message, Name, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
}
