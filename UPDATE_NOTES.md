# v2 update - new Battle / Multiplayer UI

Flow: home "Battle" -> Multiplayer menu -> "Find a Match" (left card) -> army screen "Attack!" -> scouting -> deploy -> battle -> Return Home.

First-time setup (in order):
1. python main.py  -> option 5  (calibrate game area, on the home screen)
2. option 4 on each screen (home, Multiplayer menu, army screen, battle) - the matching icon must say FOUND
3. option 6 on a scouting screen - check debug_crops/deploy_preview.png, test hotkeys
4. option 3 (you deploy) for a dry run, then option 1 / 2

Settings live in config.json (deploy points, hotkeys, spells, storage caps, battle timeout, cooldown).
Emergency stop: move the mouse into any screen corner, or Ctrl+C.

# v3 - deploy rewrite
- Keys are sent as hardware scancodes (plain pyautogui key events are ignored by Google Play Games). If cards still don't select: make sure the GPG window is focused and "Game controls" is ON, or set deploy.use_hotkeys=false (clicks the cards), or click_card_fallback=true.
- Army lives in config.json -> deploy.units (key / kind / taps / delay). Edit it when your army changes (taps = number of troops, e.g. 117).
- Troops, siege and heroes go on a ring just outside the red line on all 4 sides, new random points every tap. Spells land at random points on the base.
- All troops deploy right after the base loads (0.3s), then spells at their delays, then hero abilities.
- Tune deploy.base (cx, cy, hw, hh) + ring_margin with menu option 6 until the red diamond hugs the red line.
- Added surrender_okay_6.png and surrender_btn_8.png (cropped from your screenshot). return_home_7.png is still missing: send a crop of it, otherwise the bot taps battle.return_home_pos.

# v4 - verified, rocket-fast deploy
- The bot now READS the card bar (like your screenshot): a coloured card = units left, a grey card = empty (x0). It taps each card until it turns grey, then runs up to 4 sweep passes, and prints `OK - every card is empty` (or lists cards that are not).
- `taps` in deploy.units is now only a hint/cap, so a wrong troop count (117 vs the real one) no longer leaves troops behind or overshoots.
- Cards that are already grey/absent at the start are skipped (the old spell_s / spell_d entries were removed - your bar only has spell A).
- Clicks are sent directly through the Windows API with a 1 ms timer: ~100 taps/second (deploy.verify.tap_delay, lower = faster). The corner fail-safe still works.
- Hero abilities now fire 2.5 s after the deploy starts (was 6 s); spell A is cast 1.5 s after the troops.
- Menu option 6 now prints/draws the card patches: green = card has units, red = grey. Before the first attack, run option 6 on a scouting screen and check all cards are green; if a card is red while it clearly has units, adjust deploy.cards / card_y.
- If your existing config.json already contains a "deploy" section from an older run, delete it (or just delete config.json, keep "viewport") so the new defaults apply.
