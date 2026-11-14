using System;
using System.IO;
using Godot;

namespace Hachimi;

/// <summary>
/// Serves the embedded card portrait at <see cref="ModEntry.PortraitPath"/>.
/// The mod ships no .pck, so a resource format loader is the way to expose a
/// texture that the game's own <c>ResourceLoader.Load&lt;Texture2D&gt;</c> calls
/// can resolve.
/// </summary>
public sealed partial class HachimiPortraitLoader : ResourceFormatLoader
{
    private static readonly StringName Texture2DType = "Texture2D";
    private static readonly string[] Extensions = { "png" };

    private static HachimiPortraitLoader? _instance;
    private static ImageTexture? _texture;

    public static void Register()
    {
        if (_instance != null)
        {
            return;
        }

        _instance = new HachimiPortraitLoader();
        ResourceLoader.AddResourceFormatLoader(_instance, atFront: true);
        Log.Info("portrait loader registered");
    }

    /// <summary>Lazily decodes the embedded PNG into a texture.</summary>
    public static Texture2D GetTexture()
    {
        if (_texture != null)
        {
            return _texture;
        }

        using var stream = typeof(HachimiPortraitLoader).Assembly
            .GetManifestResourceStream("Hachimi.assets.hachimi_card.png")
            ?? throw new InvalidOperationException("embedded hachimi_card.png is missing");

        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);

        var image = new Image();
        var error = image.LoadPngFromBuffer(buffer.ToArray());
        if (error != Error.Ok)
        {
            throw new InvalidOperationException("could not decode hachimi_card.png: " + error);
        }

        _texture = ImageTexture.CreateFromImage(image);
        return _texture;
    }

    public override string[] _GetRecognizedExtensions() => Extensions;

    public override bool _HandlesType(StringName type) => type == Texture2DType;

    public override string _GetResourceType(string path) => "Texture2D";

    // Godot calls _RecognizePath with an empty type when the caller passes no
    // type hint (ResourceLoader.Load<Texture2D>(path, null)), so accept both.
    public override bool _RecognizePath(string path, StringName type) =>
        path.StartsWith(ModEntry.PortraitPath, StringComparison.Ordinal) && (type.IsEmpty || type == Texture2DType);

    public override bool _Exists(string path) =>
        path.StartsWith(ModEntry.PortraitPath, StringComparison.Ordinal);

    public override string[] _GetDependencies(string path, bool addTypes) => Array.Empty<string>();

    public override Variant _Load(string path, string originalPath, bool useSubThreads, int cacheMode)
    {
        try
        {
            return Variant.From(GetTexture());
        }
        catch (Exception e)
        {
            Log.Error("portrait load failed: " + e);
            return default;
        }
    }
}
