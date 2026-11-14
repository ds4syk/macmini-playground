using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Godot;
using MegaCrit.Sts2.Core.Entities.Cards;
using MegaCrit.Sts2.Core.Models;
using MegaCrit.Sts2.Core.Models.Cards;
using MegaCrit.Sts2.Core.Modding;
using MegaCrit.Sts2.Core.Nodes.Cards;

namespace HachimiPreview;

/// <summary>
/// QA helper (not part of the shipped mod). Renders the real NCard widget for the
/// Fool's Wah Cho Ki card and saves one in-game screenshot to /tmp:
///   /tmp/hachimi_capture_after  exists -> /tmp/hachimi_after.png
///   otherwise                        -> /tmp/hachimi_before.png
/// Run once with the Hachimi mod removed (baseline) and once with it installed
/// (patched); every other condition (mod set, viewport, node) stays identical.
/// </summary>
[ModInitializer(nameof(Initialize))]
public static class PreviewEntry
{
    private const double StartDelaySeconds = 6.0;
    private const double SettleSeconds = 1.5;

    private static readonly List<GodotObject> KeepAlive = new();

    private static SceneTree? _tree;
    private static bool _captured;
    private static ulong _startMsec;
    private static double _shownAt;
    private static CanvasLayer? _layer;
    private static NCard? _card;
    private static string _outputPath = "";

    public static void Initialize()
    {
        try
        {
            _tree = Engine.GetMainLoop() as SceneTree;
            if (_tree == null)
            {
                GD.PrintErr("[HachimiPreview] no SceneTree");
                return;
            }

            var wantsAfter = File.Exists("/tmp/hachimi_capture_after");
            _outputPath = wantsAfter ? "/tmp/hachimi_after.png" : "/tmp/hachimi_before.png";
            _startMsec = Time.GetTicksMsec();
            _tree.ProcessFrame += OnFrame;
            GD.Print("[HachimiPreview] frame driver installed -> " + _outputPath);
        }
        catch (Exception e)
        {
            GD.PrintErr("[HachimiPreview] init failed: " + e);
        }
    }

    private static void OnFrame()
    {
        try
        {
            if (_captured)
            {
                return;
            }

            var elapsed = (Time.GetTicksMsec() - _startMsec) / 1000.0;
            if (elapsed < StartDelaySeconds)
            {
                return;
            }

            if (_card == null)
            {
                var model = ModelDb.AllCards.FirstOrDefault(c => c is WahChoKi);
                if (model == null)
                {
                    return;
                }

                GD.Print("[HachimiPreview] showing " + model.Id);
                ShowCard(model);
                _shownAt = elapsed;
                return;
            }

            if (elapsed - _shownAt > SettleSeconds)
            {
                Capture(_outputPath);
                _captured = true;
                _tree!.ProcessFrame -= OnFrame;
                GD.Print("[HachimiPreview] done");
            }
        }
        catch (Exception e)
        {
            GD.PrintErr("[HachimiPreview] frame error: " + e);
            _captured = true;
            _tree!.ProcessFrame -= OnFrame;
        }
    }

    private static void ShowCard(CardModel model)
    {
        _layer = new CanvasLayer { Layer = 250 };
        _tree!.Root.AddChild(_layer);
        KeepAlive.Add(_layer);

        var bg = new ColorRect { Color = new Color(0.10f, 0.09f, 0.13f, 1f) };
        bg.SetAnchorsPreset(Control.LayoutPreset.FullRect);
        _layer.AddChild(bg);

        var label = new Label { Text = "Hachimi card preview (QA)", Position = new Vector2(40, 30) };
        label.AddThemeFontSizeOverride("font_size", 28);
        _layer.AddChild(label);

        var card = NCard.Create(model);
        if (card == null)
        {
            GD.PrintErr("[HachimiPreview] NCard.Create returned null");
            return;
        }

        _layer.AddChild(card);
        _card = card;
        KeepAlive.Add(card);

        // Pooled card nodes are not ready when Model is assigned, so Reload() bails;
        // refresh once the node is in the tree so the title/description render too.
        card.UpdateVisuals(PileType.None, CardPreviewMode.Normal);

        card.Scale = new Vector2(1.3f, 1.3f);
        var viewport = _tree!.Root.GetVisibleRect().Size;
        card.Position = new Vector2(viewport.X / 2f - 185f, 300f);
        GD.Print($"[HachimiPreview] title={model.Title} portrait={model.PortraitPath} portraitTex={(model.Portrait != null)}");
    }

    private static void Capture(string path)
    {
        var image = _tree!.Root.GetTexture().GetImage();
        var error = image.SavePng(path);
        GD.Print($"[HachimiPreview] saved {path} ({image.GetWidth()}x{image.GetHeight()}) error={error}");
    }
}
