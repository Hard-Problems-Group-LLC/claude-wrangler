// Fake Claude Code console binary for claude-wrangler's Windows shim tests.
//
// Compiled at test time with the .NET Framework compiler (csc.exe). It never
// touches the network or any profile directory:
//   --version  prints "<FAKE_CLAUDE_VERSION or 2.1.296> (Claude Code)"
//   --help     prints a usage text with a "Commands:" section
//   --settings <json> <sub> --help
//              prints "Usage: claude <sub> [options]", or fails with an
//              option error when <sub> is listed in the comma-separated
//              FAKE_CLAUDE_REJECT_SETTINGS
//   otherwise  prints one ASCII-only JSON object {"argv": [...], "env": {...}}
//              (and writes the same UTF-8 JSON to FAKE_CLAUDE_REPORT when set),
//              then exits with FAKE_CLAUDE_EXIT (default 0).
using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Text;

internal static class FakeClaude
{
    private static string Escape(string value, bool asciiOnly)
    {
        var builder = new StringBuilder("\"");
        foreach (char c in value)
        {
            if (c == '"') { builder.Append("\\\""); }
            else if (c == '\\') { builder.Append("\\\\"); }
            else if (c < 0x20 || (asciiOnly && c > 0x7e))
            {
                builder.Append("\\u").Append(((int)c).ToString("x4"));
            }
            else { builder.Append(c); }
        }
        return builder.Append('"').ToString();
    }

    private static string Report(string[] args, bool asciiOnly)
    {
        var builder = new StringBuilder("{\"argv\": [");
        for (int i = 0; i < args.Length; i++)
        {
            if (i > 0) { builder.Append(", "); }
            builder.Append(Escape(args[i], asciiOnly));
        }
        builder.Append("], \"env\": {");
        var names = new List<string>();
        IDictionary environment = Environment.GetEnvironmentVariables();
        foreach (DictionaryEntry entry in environment) { names.Add((string)entry.Key); }
        names.Sort(StringComparer.Ordinal);
        for (int i = 0; i < names.Count; i++)
        {
            if (i > 0) { builder.Append(", "); }
            builder.Append(Escape(names[i], asciiOnly)).Append(": ");
            builder.Append(Escape((string)environment[names[i]] ?? "", asciiOnly));
        }
        return builder.Append("}}").ToString();
    }

    private static int Main(string[] args)
    {
        if (args.Length > 0 && args[0] == "--version")
        {
            string version = Environment.GetEnvironmentVariable("FAKE_CLAUDE_VERSION");
            Console.Out.Write((string.IsNullOrEmpty(version) ? "2.1.296" : version) + " (Claude Code)\n");
            return 0;
        }
        if (args.Length > 0 && args[0] == "--help")
        {
            Console.Out.Write(
                "Usage: claude [options] [command] [prompt]\n\n" +
                "Options:\n  -h, --help  Display help for command\n\n" +
                "Commands:\n" +
                "  doctor                 Check the health of your installation\n" +
                "  mcp                    Configure and manage MCP servers\n" +
                "  plugin|plugins         Manage Claude Code plugins\n" +
                "  update|upgrade         Check for updates and install if\n" +
                "                         available\n");
            return 0;
        }
        if (args.Length >= 4 && args[0] == "--settings" && args[args.Length - 1] == "--help")
        {
            // The install-time `--settings` probe: `--settings <json> <sub> --help`.
            string rejecting = Environment.GetEnvironmentVariable("FAKE_CLAUDE_REJECT_SETTINGS") ?? "";
            if (Array.IndexOf(rejecting.Split(','), args[2]) >= 0)
            {
                Console.Error.Write("error: unknown option '--settings'\n");
                return 1;
            }
            Console.Out.Write("Usage: claude " + args[2] + " [options]\n");
            return 0;
        }
        string reportPath = Environment.GetEnvironmentVariable("FAKE_CLAUDE_REPORT");
        if (!string.IsNullOrEmpty(reportPath))
        {
            File.WriteAllText(reportPath, Report(args, false), new UTF8Encoding(false));
        }
        Console.Out.Write(Report(args, true) + "\n");
        string exitCode = Environment.GetEnvironmentVariable("FAKE_CLAUDE_EXIT");
        int code;
        return int.TryParse(exitCode, out code) ? code : 0;
    }
}
