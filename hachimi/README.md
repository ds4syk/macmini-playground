# 哈基米替换哇啾叽 · 杀戮尖塔 2 Mod

把愚者（The Fool）的「**哇 / 啾 / 叽**」文本全部换成「**哈 / 基 / 米**」，
做法和创意工坊里的《草泥马替换哇啾叽》（ID 3839553444）一致：**纯文本替换**，不碰卡面、数值和卡牌 ID。

| 原文 | 替换后 |
|---|---|
| 哇 啾 叽（卡名） | 哈 基 米 |
| 啾 副歌、叽 唱诵、哇 战锤……（19 张卡名） | 基 副歌、米 唱诵、哈 战锤…… |
| 打出手牌中所有「哇」和「叽」牌 | 打出手牌中所有「哈」和「米」牌 |
| 哇 / 啾 / 叽 旋律（提示与能力描述） | 哈 / 基 / 米 旋律 |
| 遗物「陶笛」、药水「瓶装歌」等文本里提到的牌名 | 同步替换 |

- **范围**：19 张卡牌卡名、卡牌描述、能力描述、旋律提示，以及相关遗物 / 药水文本里提到的名字。
- **语言**：简体中文 + 繁体中文（繁体用「嘰」，同样换成「米」）。
- **只有文本**：卡牌之间的联动走内部标签，不看名字，所以玩法、存档、联机判定完全不变；无前置 Mod。
- **额外音效**（按最初需求加的）：打出「哇 啾 叽」（现「哈 基 米」）时会播放热门哈基米语音，
  同时把这张牌原本的 哇 / 啾 / 叽 三段旋律人声静音 2.5 秒，避免叠在一起。

## 对比截图

同一个 `NCard` 控件、同一窗口尺寸和 UI 状态；基线是未安装本 Mod 的 v0.113.0。
左边是「哇 啾 叽 → 哈 基 米」，右边是「啾 副歌 → 基 副歌」，可见卡名和描述里的字都换了。

| 之前（原版） | 之后（本 Mod） |
|---|---|
| ![before](docs/evidence/before.png) | ![after](docs/evidence/after.png) |

## 目录

```
src/
  ModEntry.cs        入口 + 日志
  CardPatches.cs     文本替换补丁 + 出牌音效补丁
  HachimiSound.cs    内嵌 WAV -> AudioStreamWav -> SFX 总线
  Hachimi.json       Mod 清单
assets/hachimi_voice.wav   内嵌语音
docs/evidence/       对比截图
docs/art/            可选的哈基米卡面插画（本 Mod 默认不替换卡面，和参考 Mod 一致）
tools/make_card_art.py     插画生成脚本
tools/PreviewMod/          QA 工具：游戏内渲染卡牌并截图，不随 Mod 安装
build.sh             构建 + 安装
```

## 构建与安装

```bash
./build.sh
```

会把 `Hachimi.dll` 和 `Hachimi.json` 复制到
`…/Slay the Spire 2/SlayTheSpire2.app/Contents/MacOS/mods/Hachimi/`。

## 实现要点

- 文本替换打在 `LocManager.LoadTable` 的 Postfix 上：只处理 `zhs` / `zht` 的
  `cards`、`powers`、`relics`、`potions`、`static_hover_tips`、`card_keywords`、`card_library` 这几张表，
  把这些表里所有 `哇→哈`、`啾→基`、`叽→米`、`嘰→米`。
  这样能改到「文本里提到的牌名」，又不会误伤无关台词（例如 `好哇！！`、
  小怪 banter 里的 `哇——`、鸟的 `啾啾`、`叽叽喳喳` 等，都在别的表或已排除的 key 里）。
- 卡名、描述、能力提示都从这个本地化表读取，所以改表即可全量生效。
- 语音：`WahChoKi.OnPlay` 前缀播放内嵌哈基米语音，并在 2.5 秒窗口内让
  `SfxCmd.Play` 跳过 `event:/sfx/characters/fugitive/fugitive_{wa,cho,ki}`。

## 验证记录

- `dotnet build -c Release`：0 warning / 0 error。
- 启动日志：`[Hachimi] loading v0.2.0.0` / `voice loaded (63946 PCM bytes, 22050 Hz)` /
  `loaded: 哇 / 啾 / 叽 -> 哈 / 基 / 米`，`Finished mod initialization`。
- 游戏内截图（`docs/evidence/`，同一控件同一状态）：
  - 原版：`哇 啾 叽` / `啾 副歌`，描述里是 `所有“哇”和“叽”牌`
  - 装上后：`哈 基 米` / `基 副歌`，描述里是 `所有“哈”和“米”牌`
- 语音路径：QA 工具调用 `HachimiSound.Play()`，日志 `[Hachimi] voice clip playing`，无资源/音频报错。

## 素材与说明

- **语音**：B 站《哈基米常用音效》（[BV1JyYPzyE8w](https://www.bilibili.com/video/BV1JyYPzyE8w)）
  1.00–2.45 秒，22050 Hz 单声道 16-bit，响度归一化；仅个人本地使用。
- **卡面**：保持原版，和参考 Mod 一致。`docs/art/hachimi_card.png` 是我之前画的可选哈基米插画，
  需要的话可以加一个开关把它套到卡面上。
- **兼容性**：macOS arm64 / v0.113.0（public-beta）。「哇 啾 叽」是 v0.113 新增的愚者卡，老版本没有。
