using System;
using HarmonyLib;
using MegaCrit.Sts2.Core.Logging;
using MegaCrit.Sts2.Core.Modding;

namespace Hachimi;

/// <summary>
/// Entry point. The game calls <see cref="Initialize"/> once after loading this
/// assembly because the class carries <see cref="ModInitializerAttribute"/>.
/// </summary>
[ModInitializer(nameof(Initialize))]
public static class ModEntry
{
    public const string ModId = "Hachimi";

    private static bool _initialized;

    public static void Initialize()
    {
        if (_initialized)
        {
            return;
        }

        _initialized = true;

        try
        {
            Log.Info($"loading v{typeof(ModEntry).Assembly.GetName().Version}");

            var harmony = new Harmony(ModId);
            harmony.PatchAll(typeof(ModEntry).Assembly);

            HachimiSound.Preload();

            Log.Info("loaded: 哇 / 啾 / 叽 -> 哈 / 基 / 米");
        }
        catch (Exception e)
        {
            Log.Error("failed to initialize: " + e);
        }
    }
}

/// <summary>Thin wrapper so every message carries the mod prefix.</summary>
internal static class Log
{
    private static readonly Logger Logger = new(ModEntry.ModId, LogType.Generic);

    public static void Info(string message) => Logger.Info(message);

    public static void Warn(string message) => Logger.Warn(message);

    public static void Error(string message) => Logger.Error(message);
}
