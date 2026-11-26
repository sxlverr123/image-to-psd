# PSD Maker

![Python](https://img.shields.io/badge/Python-3.8%2B-blue?logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

Turn any photo into an **editable `.psd`** file, either as a single layer or split into real layers (with names, groups, and blend modes). It can also **find a logo or image in a photo and replace it** with another one, automatically.

Works with Photoshop, GIMP, Krita, Photopea, and anything else that opens PSD files.

## Files

| File | What it is |
| :--- | :--- |
| `psd_maker.py` | The tool (window + command line) |
| `install_dependencies.bat` | Installs everything the tool needs |
| `README.md` | This file |

## Setup (once)

1. Install [Python 3.8+](https://www.python.org/downloads/) (on Windows tick **"Add python.exe to PATH"**).
2. Double-click `install_dependencies.bat` and wait until it says *All set*.
3. Double-click `psd_maker.py`. The window opens.

> [!NOTE]
> If you skip step 2, the tool installs `pillow` and `numpy` by itself the first time it starts, but the extra features (subject, lines, logo replacement) need `opencv-python`.

## Using the window

1. **Choose photo(s)** — one or many.
2. Pick **what you want**:
   - *PSD only* — a single editable layer.
   - *Everything* — subject + tones + colors + lines, each in its own group.
   - Or one specific split (see below).
3. Press **Create PSD**. The `.psd` is saved next to the photo (or in the output folder you chose).

### Replace a logo / image

1. Choose the scene photo(s).
2. Mode: **Replace logo / image**.
3. **To replace** = a photo or crop of the logo you want to change. You do not select anything inside the scene, the tool finds it.
4. **New image** = what to put there (PNG with transparency works best).
5. Create PSD. In the result you get a `Replacements` group; for every copy found there is a `Cleanup` layer (old logo erased) and a `New image` layer you can move, scale, or edit freely.

> [!TIP]
> If nothing is found, lower **Minimum similarity** (try `0.5`). If it replaces things it should not, raise it.

## Split methods

| Method | Layers you get |
| :--- | :--- |
| `flat` | One layer, "Photo" |
| `subject` | Subject cut out + Background |
| `tones` | Shadows / Midtones / Highlights as luminosity masks |
| `colors` | One layer per dominant color (`-k` sets how many, `2`-`32`) |
| `edges` | Photo + a "Lines" outline layer (multiply) |
| `channels` | Red / Green / Blue on separate layers (Screen blend) |
| `frequency` | Color (low frequencies) + Texture (high frequencies) for retouching |
| `all` | `subject` + `tones` + `colors` + `edges`, each in a group (only the first is visible) |

Every split (except `edges` and `all` combos) stacks back into the **exact original photo**, and a hidden "Original" layer is included at the bottom for reference (turn it off with *Include the original photo*). `channels` and `frequency` can't be combined with other methods.

## Command line

```bash
python psd_maker.py                           # opens the window
python psd_maker.py photo.jpg                 # PSD only
python psd_maker.py photo.jpg -m tones
python psd_maker.py photo.jpg -m subject,tones
python psd_maker.py *.jpg -m colors -k 8 -o out_folder
python psd_maker.py photo.jpg -m subject --rect 0.2,0.1,0.6,0.8

# replace a logo (repeat --replace for several)
python psd_maker.py scene.jpg --replace old_logo.png new_logo.png
python psd_maker.py scene.jpg --replace a.png b.png --replace c.png d.png --threshold 0.55
```

**Useful options:** `--flat-colors`, `--no-original`, `--raw` (no compression, maximum compatibility), `--max-side 2000` (shrink huge photos), `--stretch` and `--no-clean` (for `--replace`).

## Tips

- **Subject cut-out** is rough without extras. Install `rembg` (the installer asks) for clean edges, or give the area with `--rect x,y,w,h` (fractions of the image).
- **Replacing logos** works best when the "to replace" image looks like the logo in the scene (same colors, not mostly hidden). Different sizes are fine; rotated or tilted copies are found too, as long as the logo has enough detail.
- The cleanup uses inpainting: great on plain or smooth backgrounds, it can leave a soft patch on very busy ones. You can always paint over the `Cleanup` layer or delete it.
- If a program refuses to open the file, create it again with `--raw`.
- The tool never overwrites your source file; if the output name matches the input, it adds `_edited`.

## Troubleshooting

| Problem | Fix |
| :--- | :--- |
| Double-click does nothing / error box | Run `install_dependencies.bat`; make sure `psd_maker.py` is in the same folder |
| `"tkinter missing"` | **Windows/macOS:** reinstall Python from [python.org](https://www.python.org/). **Linux:** `sudo apt install python3-tk` |
| `"needs opencv-python"` | `pip install opencv-python` |
| Not found when replacing | Lower the similarity (`0.45`-`0.55`) or use a cleaner crop of the logo |
| Big photo is slow | Use `--max-side 2000` |

## Limits

- 8-bit RGB only; maximum `30000` px per side.
- Photos with transparency are flattened on white.
- Opening an existing `.psd` as input reads only its flattened picture, not its layers.
