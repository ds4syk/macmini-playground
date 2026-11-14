using System.Collections.Generic;
using HarmonyLib;
using MegaCrit.Sts2.Core.Commands;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Cards;

namespace Hachimi;

/// <summary>
/// Re-skins the Fool's 哇 啾 叽 (Wah Cho Ki) card:
/// title, card portrait and the melody voice line all become 哈基米.
/// Mechanics, rarity, tags and card id are intentionally untouched so runs,
/// saves and multiplayer stay compatible.
/// </summary>
[HarmonyPatch(typeof(CardModel), "get_Title")]
internal static class CardTitlePatch
{
    [HarmonyPrefix]
    private static bool Prefix(CardModel __instance, ref string __result)
    {
        if (__instance is not WahChoKi)
        {
            return true;
        }

        // Keep the vanilla upgrade suffix ("+"/"+N") so upgraded cards still read correctly.
        var title = ModEntry.CardTitle;
        if (!__instance.IsUpgraded)
        {
            __result = title;
        }
        else if (__instance.MaxUpgradeLevel > 1)
        {
            __result = $"{title}+{__instance.CurrentUpgradeLevel}";
        }
        else
        {
            __result = title + "+";
        }

        return false;
    }
}

[HarmonyPatch(typeof(CardModel), "get_PortraitPath")]
internal static class CardPortraitPathPatch
{
    [HarmonyPrefix]
    private static bool Prefix(CardModel __instance, ref string __result) => TryOverride(__instance, ref __result);

    internal static bool TryOverride(CardModel instance, ref string result)
    {
        if (instance is not WahChoKi)
        {
            return true;
        }

        result = ModEntry.PortraitPath;
        return false;
    }
}

[HarmonyPatch(typeof(CardModel), "get_BetaPortraitPath")]
internal static class CardBetaPortraitPathPatch
{
    [HarmonyPrefix]
    private static bool Prefix(CardModel __instance, ref string __result) =>
        CardPortraitPathPatch.TryOverride(__instance, ref __result);
}

[HarmonyPatch(typeof(CardModel), "get_PortraitPngPath")]
internal static class CardPortraitPngPathPatch
{
    [HarmonyPrefix]
    private static bool Prefix(CardModel __instance, ref string __result) =>
        CardPortraitPathPatch.TryOverride(__instance, ref __result);
}

[HarmonyPatch(typeof(CardModel), "get_AllPortraitPaths")]
internal static class CardAllPortraitPathsPatch
{
    [HarmonyPrefix]
    private static bool Prefix(CardModel __instance, ref IEnumerable<string> __result)
    {
        if (__instance is not WahChoKi)
        {
            return true;
        }

        __result = new[] { ModEntry.PortraitPath };
        return false;
    }
}

/// <summary>Plays the 哈基米 voice line and mutes the card's 哇/啾/叽 voice lines.</summary>
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
            Log.Info("played 哈基米 voice for 哇 啾 叽");
        }
        catch (System.Exception e)
        {
            Log.Error("voice playback failed: " + e);
        }
    }
}

[HarmonyPatch(typeof(SfxCmd), "Play", new[] { typeof(string), typeof(float) })]
internal static class MelodySfxSuppressorPatch
{
    [HarmonyPrefix]
    private static bool Prefix(string sfx)
    {
        return !HachimiSound.ShouldSuppressMelody(sfx);
    }
}
