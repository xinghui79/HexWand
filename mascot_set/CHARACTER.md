# 看板娘角色卡 · mascot_set 素材包

> 包状态（2026-09-19）：自包含，可整包拷走。成品图（表情五连 + 侧身两张）
> 全部按本卡生成、已过验收清单，无遗留旧图。待扩展：Q版 / 换装 / 动作差分
> （另起批次；Live2D 分图层 AI 做不了，需 PSD 手工拆）。
>
> 注：`assets/mascot.png` 是 HUD 运行时文件；本包的 `mascot_original.png`
> 是它的副本，两边互不影响。

## 用法：三步，缺一必漂

1. **贴块**：下方"固定描述块"整段原样粘贴，一个词不改；变体只能在块后
   **追加**一句（见追加示例）。
2. **带图**：上传下表全部 6 张 ✅ 参考图（有"角色参考/垫图"功能的工具
   再叠上）。
3. **验收**：出图后过"验收清单"，错一项重roll；换视角先查"分视角规则"。

## 文件清单

| 文件 | 角色 | 生成时上传 |
| --- | --- | --- |
| `mascot_original.png` | 最初原版（脸/气质/仓鼠终极基准） | ✅ |
| `fullbody.png` | 全身基准（构图/服装硬件） | ✅ |
| `ref_face.png` | 脸部正卡（五官/发色/马尾方位） | ✅ |
| `ref_hair.png` | 头发三视图（前/左/后，方位互证） | ✅ |
| `ref_outfit_sheet.png` | 服装平铺定稿（外套前/背+短裤+鞋+腿环） | ✅ |
| `ref_hamster_sheet.png` | 仓鼠三视图定稿（前/侧/后） | ✅ |
| `expr_happy/angry/shy/surprised/cold.png` | 成品：全身表情五连 | — |
| `sideview_left.png` / `sideview_right.png` | 成品：全身侧身（面朝左/面朝右） | — |

**事实源优先级**（图与文字冲突时裁决）：`mascot_original.png`（脸/仓鼠）
＞ `fullbody.png`（构图/硬件）＞ 两张 sheet（部件细节）＞ 描述块文字
——文字是图的摘要，以图为准。

## 固定描述块（整段粘贴，一字不改）

```
A late-teen anime girl character, consistent across every generation:
- HAIR: silver-lavender with soft pink iridescence; two small round space
  buns on top of the head; ONE long wavy side ponytail on her left side —
  ALWAYS verify by image position: front-facing = RIGHT side of the image,
  back view = LEFT side of the image; tied with a plain dark charcoal
  fabric scrunchie. Loose face-framing bangs. No other hair ornaments.
  NO earrings, NO hair clips.
- EYES: large round violet-purple gradient eyes with white sparkle
  highlights.
- FACE: light blush; when smiling, ONE small pointed canine fang
  (her upper right). No other distinguishing marks.
- OUTFIT: dark charcoal (near-black) slightly oversized high-collar tech
  jacket with a SILVER-GREY main zipper and a triangular ring pull at the
  collar; ONE vertical indigo zip pocket on her LEFT chest with a small
  indigo triangle above and a larger indigo triangle below; an indigo
  diagonal stripe band on HER RIGHT upper sleeve (LEFT side of a
  front-facing image); a flap cargo pocket with strap-and-buckle on her
  LEFT sleeve; two angled waist pockets with indigo-trimmed zippers; a
  long indigo belt tail with buckle hanging at her LEFT hip. Plain black
  mid-length shorts with a long indigo belt tail hanging down. Black thigh
  garter band with rectangular buckle on her LEFT thigh. White chunky
  sneakers with thick indigo-purple soles and an indigo square patch with
  a small white X logo on the tongue. Short white socks.
- COMPANION: a tiny round fluffy white hamster-like creature — spiky fur
  tufts on the head, large round ears with pink inner fill, black bead
  eyes with white highlights, pink nose, happy open smile, thin grey
  whiskers, small pink paws with toe beans, long thin pink tail curled
  into an S, soft lavender shading on the lower body. Floats beside one of
  her shoulders (either side is fine — position not fixed, owner decision
  2026-09-19). Include only if the shot calls for it.
- STYLE: soft pastel anime illustration, clean line art, gentle cel
  shading, high detail, pure white background, no text, no watermark.
```

追加示例（只加在块后，不动块内）：

```
Expression & pose: HAPPY — beaming closed-eye smile with the single fang
showing, both arms raised in cheer.
```

```
Outfit variant: oversized hoodie pajamas, same hair and face.
```

## 验收清单（出图逐项对）

1. 丸子头 ×2；侧马尾方位符合当前视角（见"分视角规则"）；深色布发圈
2. 无耳坠、无发卡、无额外头饰
3. 紫渐变瞳带高光；笑时**只有一颗**虎牙
4. 发色银偏紫（非纯白/纯粉），冷暖与 fullbody.png 一致
5. 外套：银灰主链+领口三角环拉头；左胸靛竖袋+上下双三角标；靛条带在
   **她的右臂**；左袖扣带货袋；靛紫腰带尾垂**她的左胯**
6. 腿环在**她的左大腿**，黑色带方扣
7. 鞋：白面厚底、靛紫大底、鞋舌靛方块+白 X 标
8. 有仓鼠则：圆球白毛、顶尖毛簇、粉内耳、粉 S 长尾（左右肩不限）
9. 纯白底、无文字、无水印

## 规则

### 通用

- **方位一律按画面写死**："her left side" 会被画到另一边（实测）→ 必须标
  "front-facing = RIGHT side of the image" 这类画面坐标。
- **禁幻觉配角**：要尺寸参考时 AI 会自己长出别的角色（实测冒出粉裙女孩）
  → 追加 `no humans, no other characters`。
- **耳坠惯犯**：表情图曾违规长出耳坠 → 重roll 追加 `no earrings`。
- **部件 sheet 轻易不重roll**：服装平铺两轮生成实测都漂（银链→靛链、
  肩条换臂、短裤长扣带、跑鞋变板鞋）；现存两张是核对过的定稿。真要重出：
  逐项抄 OUTFIT/COMPANION 段 + 对照 fullbody.png 过清单。
- 口袋/绑带**数量**允许小幅浮动；**颜色、银主链、高领、条带位置**必须锁。

### 分视角规则（写提示词前先查这张表）

| 视角 | 马尾 | 近侧袖 | 远侧袖 | 腿环 |
| --- | --- | --- | --- | --- |
| 正面 | 图像右侧 | 她左袖=图像右：货袋 | 她右袖=图像左：条带 | 图像右侧大腿 |
| 背面 | 图像左侧 | — | — | 可见（她左腿） |
| 面朝左（观众看她左侧） | 沿背清晰可见 | 她左袖：ONLY 货袋 | 不可见 | 近腿清晰 |
| 面朝右（观众看她右侧） | 藏颈后只露梢 | 她右袖：ONLY 条带 | 不可见 | 近腿光腿，远侧露一线 |
| 侧身通用 | — | 近侧袖写 "ONLY …"，远侧元素写 "must NOT be visible"，否则两臂混画（实测） | | |
