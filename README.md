# BonkScanner Deck

A [Decky Loader](https://decky.xyz) plugin that rerolls **Megabonk** maps on the
Steam Deck until the map matches your target: Moais, Microwaves, Shady Guys,
Boss Curses, Challenges, Magnet Shrines.

It is a Linux port of the reroll engine of
[BonkScanner](https://github.com/ALuiell/BonkScanner) by ALuiell (GPL-3.0). It
reads the map counters from game memory and holds the in-game quick-reset key
(R) through a virtual keyboard, exactly like the Windows app does.

## Requirements

- Steam Deck (or another SteamOS / Linux PC) with **Decky Loader** installed.
- Megabonk running through **Proton** (the Windows build). The memory offsets
  are those of the Windows build; the native Linux build is not supported.
  Steam → Megabonk → Properties → Compatibility → *Force the use of a specific
  Steam Play compatibility tool* → pick Proton.
- The quick-reset key in Megabonk left at **R** (the default).

## Install

1. Copy the link to the latest zip:
   `https://github.com/lightview/bonkscanner-deck/releases/latest/download/bonkscanner-deck.zip`
2. On the Deck open the Quick Access menu (`…`) → Decky (plug icon) → ⚙ Settings.
3. **General → Developer mode**: turn it on. A *Developer* tab appears.
4. **Developer → Install Plugin from URL**: paste the zip link → Install.
   (Or *Install Plugin from ZIP File* if you copied the zip to the Deck.)
5. Confirm the install. *BonkScanner Deck* appears in the Decky list.

The plugin asks for root because reading another process's memory and creating
a virtual keyboard require it.

### Установка (по-русски)

1. На Deck в игровом режиме: `…` → Decky (вилка) → ⚙ → **General** → включить **Developer mode**.
2. **Developer → Install Plugin from URL** → вставить
   `https://github.com/lightview/bonkscanner-deck/releases/latest/download/bonkscanner-deck.zip` → Install.
3. Megabonk должен запускаться через Proton (Свойства → Совместимость → Proton).
4. В игре: начать забег, выставить условия в плагине, нажать **R4** (задняя кнопка) — пошли рестарты; ещё раз **R4** — стоп.

## Use

1. Start a run in Megabonk (stage 1).
2. Open `…` → BonkScanner Deck, set the target with the sliders.
3. Close the menu and press the back-button hotkey (**R4** by default) in game,
   or press **Start rerolling** and close the menu within the start delay (3 s by
   default) — key presses go to the Steam overlay while it is open.
   Press the hotkey again to stop.
4. When a matching map is found the game is paused and a notification pops up.
   Open the plugin and press **Stop** any time to stop early.

Notes:

- The current map is checked first; if it already matches nothing is touched.
  Turn on *Reroll the current map too* to always get a fresh one.
- If you pause the game while rerolling, the plugin waits until you resume.
- It refuses to reset a run that is past stage 1.
- The hold time of R is read from Megabonk's own *quick reset time* setting;
  lowering that setting in the game makes rerolls faster.

## Command line

`py_modules/bonk_deck.py` also works on its own, which is handy for debugging:

```
sudo python3 bonk_deck.py --probe              # print the current map
sudo python3 bonk_deck.py --test-key           # hold R once after 5 s
sudo python3 bonk_deck.py --moai 4 --micro 2   # reroll until matched
```

## Build

```
npm install
npm run package      # -> out/bonkscanner-deck.zip
```

Pushing a tag `v*` makes GitHub Actions build the zip and attach it to a release.

## License

GPL-3.0, like BonkScanner which this is derived from. See [LICENSE](LICENSE).
