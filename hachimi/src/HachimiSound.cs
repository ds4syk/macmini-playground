using System;
using System.IO;
using System.Text;
using Godot;

namespace Hachimi;

/// <summary>
/// Plays the embedded 哈基米 voice clip and temporarily replaces the Fool's
/// "fugitive_wa / fugitive_cho / fugitive_ki" melody voice lines while the
/// 哇 啾 叽 card is being played, so the card now says 哈基米 instead.
/// </summary>
internal static class HachimiSound
{
    private const string VoiceResource = "Hachimi.assets.hachimi_voice.wav";
    private const string MelodySfxPrefix = "event:/sfx/characters/fugitive/fugitive_";

    private static AudioStreamWav? _stream;
    private static ulong _suppressMelodyUntilMsec;

    public static void Preload()
    {
        try
        {
            _stream = BuildStream();
            Log.Info($"voice loaded ({_stream.Data.Length} PCM bytes, {_stream.MixRate} Hz, stereo={_stream.Stereo})");
        }
        catch (Exception e)
        {
            Log.Error("could not load voice clip: " + e);
        }
    }

    /// <summary>
    /// Keeps the original 哇/啾/叽 melody voice lines quiet for a short window so the
    /// 哈基米 line is not buried under them. Called when the card starts being played.
    /// </summary>
    public static void BeginMelodyReplacementWindow()
    {
        _suppressMelodyUntilMsec = Time.GetTicksMsec() + 2500;
    }

    public static bool ShouldSuppressMelody(string sfx)
    {
        return Time.GetTicksMsec() < _suppressMelodyUntilMsec && sfx.StartsWith(MelodySfxPrefix, StringComparison.Ordinal);
    }

    public static void Play()
    {
        if (_stream == null)
        {
            Preload();
        }

        if (_stream == null)
        {
            return;
        }

        if (Engine.GetMainLoop() is not SceneTree { Root: { } root })
        {
            Log.Warn("no scene tree root to attach the voice player to");
            return;
        }

        var player = new AudioStreamPlayer
        {
            Stream = _stream,
            Bus = "SFX",
            VolumeDb = 0f,
        };

        root.AddChild(player);
        player.Finished += () =>
        {
            if (GodotObject.IsInstanceValid(player))
            {
                player.QueueFree();
            }
        };
        player.Play();
        Log.Info("voice clip playing");
    }

    private static AudioStreamWav BuildStream()
    {
        var bytes = ReadEmbedded(VoiceResource);

        // Minimal RIFF/WAVE reader: we only need fmt (rate/channels/bits) and data.
        int channels = 1;
        int rate = 22050;
        int bits = 16;
        byte[]? pcm = null;

        int pos = 12; // "RIFF" + size + "WAVE"
        while (pos + 8 <= bytes.Length)
        {
            string chunkId = Encoding.ASCII.GetString(bytes, pos, 4);
            int chunkSize = BitConverter.ToInt32(bytes, pos + 4);
            int chunkStart = pos + 8;

            if (chunkId == "fmt " && chunkStart + 16 <= bytes.Length)
            {
                channels = BitConverter.ToInt16(bytes, chunkStart + 2);
                rate = BitConverter.ToInt32(bytes, chunkStart + 4);
                bits = BitConverter.ToInt16(bytes, chunkStart + 14);
            }
            else if (chunkId == "data")
            {
                int available = bytes.Length - chunkStart;
                int length = chunkSize <= 0 || chunkSize > available ? available : chunkSize;
                pcm = new byte[length];
                Array.Copy(bytes, chunkStart, pcm, 0, length);
            }

            if (chunkSize <= 0)
            {
                break;
            }

            pos = chunkStart + chunkSize + (chunkSize & 1);
        }

        if (pcm == null || pcm.Length == 0)
        {
            throw new InvalidOperationException("voice clip has no data chunk");
        }

        return new AudioStreamWav
        {
            Data = pcm,
            Format = bits == 8 ? AudioStreamWav.FormatEnum.Format8Bits : AudioStreamWav.FormatEnum.Format16Bits,
            MixRate = rate,
            Stereo = channels == 2,
            LoopMode = AudioStreamWav.LoopModeEnum.Disabled,
        };
    }

    private static byte[] ReadEmbedded(string name)
    {
        using var stream = typeof(HachimiSound).Assembly.GetManifestResourceStream(name)
            ?? throw new InvalidOperationException("embedded resource is missing: " + name);
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return buffer.ToArray();
    }
}
