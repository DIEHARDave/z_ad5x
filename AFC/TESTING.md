# Testing full AFC on Zmod (AD5X)

The `feat/full-afc` branch replaces Zmod's built-in AFC emulation with the real
[AFC-Klipper-Add-On](https://github.com/AFCProject/AFC-Klipper-Add-On), with the IFS
working as an AFC unit. It's based on the current Zmod 1.7 (1.7.6).

This is a test branch. It can be rebased or force-pushed, so update it the way
step 3 shows.

## Requirements

- An AD5X on Zmod 1.7.
- The native screen turned off (`DISPLAY_OFF`). Full AFC only starts without it.
  HelixScreen, Fluidd and Mainsail all work.
- If you use the `zmod_afcplus` plugin, turn it off first:
  `DISABLE_PLUGIN NAME=zmod_afcplus`

## 1. Switch Zmod to the test branch

SSH into the printer (user `root`, password `root`) and run:

```sh
chroot /usr/data/.mod/.zmod sh -c "cd /usr/data/zmod/zmod && \
  git remote set-url origin https://github.com/DIEHARDave/z_ad5x.git && \
  git fetch origin +feat/full-afc:refs/remotes/origin/feat/full-afc && \
  git checkout -B feat/full-afc origin/feat/full-afc && git log --oneline -1"
```

The last line printed should be the newest commit on `feat/full-afc`.

## 2. Turn on full AFC

In the Fluidd/Mainsail console:

```
SAVE_ZMOD_DATA AFC=2
```

Then reboot the printer. After the reboot, AFC checks every lane. The AFC panel
should list `lane1`–`lane4`, with the loaded slots marked "LOCKED AND LOADED".

You can also find the setting in the Zmod settings menu, page 2:
**"AFC panel off (AFC-Klipper-Add-On)"**.

## 3. Updating to newer test builds

Don't use **Update** or **Recover** for Z-Mod in Fluidd/Mainsail. Moonraker still
expects ghzserg's repo, and Recover would reset you to it. Instead, run this over
SSH and then reboot:

```sh
chroot /usr/data/.mod/.zmod sh -c "cd /usr/data/zmod/zmod && \
  git fetch origin +feat/full-afc:refs/remotes/origin/feat/full-afc && \
  git reset --hard origin/feat/full-afc && git log --oneline -1"
```

This also works when the branch has been rebased or force-pushed. Moonraker will
warn "Repo not on official remote/branch" for Z-Mod. That's expected.

## 4. Going back to normal Zmod

In the console:

```
SAVE_ZMOD_DATA AFC=1
```

Then over SSH:

```sh
chroot /usr/data/.mod/.zmod sh -c "cd /usr/data/zmod/zmod && \
  git remote set-url origin https://github.com/ghzserg/z_ad5x.git && \
  git fetch origin +1.7:refs/remotes/origin/1.7 && \
  git checkout -B 1.7 origin/1.7"
```

Reboot. If you turned off `zmod_afcplus`, turn it back on with
`ENABLE_PLUGIN NAME=zmod_afcplus`.

## What to test and report

- Idle load and unload from the AFC panel, `T0`–`T3`, and lane mapping (`SET_MAP`).
- Multi-color prints, including the first load and color changes.
- Runout and infinite spool (`runout_lane`).
- Spoolman: `SET_SPOOL_ID`, and NFC tags through FF_NFC or LaneTag.

**Known limitation:** cutting filament mid-print can trigger Zmod's IFS jam pause
instead of a runout. That's Zmod's IFS motion-sensor logic.

**When reporting a problem,** include `/usr/data/logs/AFC.log`, the time it
happened, and what you did.
