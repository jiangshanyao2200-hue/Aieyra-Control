// Windows GUI launcher. No console, installation or global runtime required.
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

internal static class Launcher {
    private static string Quote(string value) {
        return "\"" + System.Text.RegularExpressions.Regex.Replace(value, "(\\\\*)\"", "$1$1\\\"").TrimEnd('\\') +
            new string('\\', value.Length - value.TrimEnd('\\').Length) + new string('\\', value.Length - value.TrimEnd('\\').Length) + "\"";
    }
    [STAThread] private static int Main(string[] args) {
        try {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string electron = Path.Combine(root, "runtime", "electron", "electron.exe");
            string python = Path.Combine(root, "runtime", "python", "python.exe");
            if (!File.Exists(electron) || !File.Exists(python)) throw new IOException("请完整解压软件文件夹后再启动。");
            var start = new ProcessStartInfo(electron) {
                Arguments = Quote(Path.Combine(root, "desktop")) + " " + Quote("--python=" + python) + " " + String.Join(" ", args.Select(Quote)),
                WorkingDirectory = root, UseShellExecute = false, CreateNoWindow = true
            };
            start.EnvironmentVariables.Remove("ELECTRON_RUN_AS_NODE");
            Process.Start(start);
            return 0;
        } catch (Exception error) {
            MessageBox.Show(error.Message, "Aieyra Control", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
