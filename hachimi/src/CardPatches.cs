using System;
using System.Collections.Generic;
using System.IO;
using HarmonyLib;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Localization;
using MegaCrit.Sts2.Core.Models.Cards;

namespace Hachimi;

/// <summary>
/// Pure text replacement, mirroring the "草泥马替换哇啾叽" style mod: every
/// 哇 / 啾 / 叽 (and the traditional 嘰) in the Fool's card names, card
/// descriptions, power descriptions, melody hints and the relic/potion text
/// that mentions those cards becomes 哈 / 基 / 米.
/// Card ids, tags, costs and numbers are untouched, so gameplay is unchanged.
/// </summary>
[HarmonyPatch(typeof(LocManager), "LoadTable", new[] { typeof(string) })]
internal static class FoolTextPatch
{
    /// <summary>Tables that hold the Fool's names and the texts that mention them.</summary>
    private static readonly HashSet<string> Tables = new(StringComparer.OrdinalIgnoreCase)
    {
        "cards.json",
        "powers.json",
        "relics.json",
        "potions.json",
        "static_hover_tips.json",
        "card_keywords.json",
        "card_library.json",
    };

    /// <summary>"嘎啊，啾啾！" is a bird chirp, not a melody syllable.</summary>
    private static readonly HashSet<string> SkippedKeys = new(StringComparer.Ordinal)
    {
        "BYRDPIP.readyToEvolve",
    };

    private static readonly char[] Syllables = { '哇', '啾', '叽', '嘰' };

    [HarmonyPostfix]
    private static void Postfix(string path, Dictionary<string, string> __result)
    {
        if (__result == null || !IsLocalizedFoolTable(path))
        {
            return;
        }

        foreach (var key in new List<string>(__result.Keys))
        {
            if (SkippedKeys.Contains(key))
            {
                continue;
            }

            var value = __result[key];
            var replaced = ReplaceSyllables(value);
            if (!ReferenceEquals(replaced, value))
            {
                __result[key] = replaced;
            }
        }
    }

    private static bool IsLocalizedFoolTable(string path)
    {
        if (path == null || (!path.Contains("/zhs/") && !path.Contains("/zht/")))
        {
            return false;
        }

        return Tables.Contains(Path.GetFileName(path));
    }

    private static string ReplaceSyllables(string value)
    {
        if (value == null || value.IndexOfAny(Syllables) < 0)
        {
            return value!;
        }

        return value
            .Replace('哇', '哈')
            .Replace('啾', '基')
            .Replace('叽', '米')
            .Replace('嘰', '米');
    }
}

/// <summary>Plays the 哈基米 voice line when the renamed card is played.</summary>
[HarmonyPatch(typeof(WahChoKi), "OnPlay")]
internal static class WahChoKiPlayPatch
{
    [HarmonyPrefix]
    private static void Prefix()
    {
        try
        {
            HachimiSound.BeginMelodyReplacementWindow();
            HachimiSound.Play();
            Log.Info("played 哈基米 voice for 哈 基 米");
        }
        catch (Exception e)
        {
            Log.Error("voice playback failed: " + e);
        }
    }
}

/// <summary>Keeps the vanilla 哇/啾/叽 voice lines quiet while the 哈基米 line plays.</summary>
[HarmonyPatch(typeof(SfxCmd), "Play", new[] { typeof(string), typeof(float) })]
internal static class MelodySfxSuppressorPatch
{
    [HarmonyPrefix]
    private static bool Prefix(string sfx)
    {
        return !HachimiSound.ShouldSuppressMelody(sfx);
    }
}
